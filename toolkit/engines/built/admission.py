#!/usr/bin/env python3
"""Admission gate for agent-built engines (docs/design/agent-built-engines.md §4 & §5).

    python3 toolkit/engines/built/admission.py --self-test

Deterministic harness verification of an immutable content-addressed snapshot against the signed
`engine.build` request, spec, and dossier:
- Public + hidden + real-unit typed domain generators with opaque IDs and invariance checks
- Static checks S1-S4 (before any run)
- Run checks A1-A11:
  - A1: conformance (`contract.check_engine` + exact card/parameter/output checks)
  - A2: determinism across processes, `PYTHONHASHSEED`, case order, and `seed_test_setting`
  - A3: `run_batch` vs `run` parity (skipped when no batch path exists)
  - A4: analytic/oracle reductions (`oracles.py`) + absent-component & N-invariance identities
  - A5: mass balance, population conservation, and compartment transition-count witness (`starsim_module`)
  - A6: sensitivity wiring over signed `(input, output)` pairs + `meta.param_reads`
  - A7: declared directions on every A6 pair (never vacuous)
  - A8: trial benchmarks (`benchmark.py`) + built-engine coverage policy (`benchmark_coverage.gate(..., policy="built")`)
  - A9: literature/dossier value benchmarks (gating only for `gating_benchmarks`)
  - A10: identifiability via `calibrators.calibrate_as_run`
  - A11: independent reference equivalence (deterministic tolerance or stochastic 95% CRN CI with `inconclusive`)
- Overfit detection (`engine_overfit_suspected` HIGH when public passes and hidden fails)
- Hidden-seed commitment (`engine_build/.hidden/seed.json`), reuse key (`compute_reuse_key`,
  `try_reuse_admission`), authoritative `engine_build/bench/*`, `engine_build/admission_sheet.md`,
  and atomic `engine_build/admission.json`. Nothing here names a disease.
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
from toolkit.engines.built import candidate as BCAND  # noqa: E402
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
    wins = (obj.get("windows") if isinstance(obj, Mapping) else None) or {}
    if not wins:
        req_wins = (req.get("outputs") or {}).get("windows") or {}
        wins = {str(w): [win[0], win[1]] for w in req_wins}
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
        if isinstance(ref_vec, (list, tuple)) and len(ref_vec) == ln:
            scale = float(rng.uniform(0.6, 1.4))
            return [max(1.0, float(x) * scale) for x in ref_vec]
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
        base = [float(x) for x in ref_vec] if isinstance(ref_vec, (list, tuple)) and len(ref_vec) == ln else [1000.0] * ln
        return [
            [x * 0.5 for x in base],
            [x * 0.85 for x in base],
            [x * 1.15 for x in base],
            [x * 1.5 for x in base],
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


def _real_units_and_packages(spec_raw: Mapping, req: Mapping, *, base_dir: str | None = None,
                             max_units: int = 200, max_packages: int = 64,
                             rng: np.random.Generator | None = None) -> tuple[list[UnitSpec], list[Package]]:
    """Extract the signed spec's real decision units and packages (stratified + extremes when > cap)."""
    rng = rng or np.random.default_rng(0)
    attr_specs = req.get("unit_attributes") or {}
    comp_specs = req.get("components") or {}
    du = spec_raw.get("decision_units") if isinstance(spec_raw, Mapping) else None
    raw_units: list[dict] = []
    if isinstance(du, Mapping):
        if isinstance(du.get("units_inline"), list):
            raw_units = [dict(u) for u in du["units_inline"] if isinstance(u, Mapping)]
        elif du.get("csv") and base_dir:
            import csv
            csv_p = os.path.join(base_dir, str(du["csv"]))
            if os.path.isfile(csv_p):
                with open(csv_p, encoding="utf-8", newline="") as f:
                    raw_units = list(csv.DictReader(f))
    id_col = str((du or {}).get("id") or (du or {}).get("id_column") or "unit_id") if isinstance(du, Mapping) else "unit_id"
    ea = (du or {}).get("engine_attributes") if isinstance(du, Mapping) else {}
    ea = ea if isinstance(ea, Mapping) else {}
    ab = req.get("attribute_binding") or {}
    cal = spec_raw.get("calibration") if isinstance(spec_raw, Mapping) else {}
    cal_attr = str(cal.get("attribute") or "").strip() if isinstance(cal, Mapping) else ""
    cal_scale = str(cal.get("scale") or "linear") if isinstance(cal, Mapping) else "linear"
    cal_range = cal.get("range") if isinstance(cal, Mapping) else None

    def _coerce_attr(raw_val, aspec: Mapping):
        at = aspec.get("type")
        if at == "continuous":
            return float(raw_val)
        if at == "integer":
            return int(round(float(raw_val)))
        if at == "vector":
            if isinstance(raw_val, str):
                return [float(x) for x in json.loads(raw_val)]
            if isinstance(raw_val, (list, tuple)):
                return [float(x) for x in raw_val]
        return raw_val

    units: list[UnitSpec] = []
    for idx, row in enumerate(raw_units):
        uid = str(row.get(id_col) or f"unit_{idx}")
        attrs: dict = {}
        for aname, aspec in attr_specs.items():
            bind = ea.get(aname)
            if isinstance(bind, Mapping) and "value" in bind:
                val = copy.deepcopy(bind["value"])
            elif isinstance(bind, Mapping) and "column" in bind:
                col = bind["column"]
                if col not in row:
                    raise ValueError(f"real unit {uid!r}: missing bound column {col!r} for attribute {aname!r}")
                raw_cell = row[col]
                if "split" in bind:
                    if "empty" in bind and str(raw_cell).strip() == str(bind["empty"]).strip():
                        val = []
                    else:
                        val = [float(x) for x in str(raw_cell).split(bind["split"])]
                else:
                    val = _coerce_attr(raw_cell, aspec)
            elif isinstance(bind, Mapping) and "columns" in bind:
                cols = bind["columns"]
                if isinstance(cols, Mapping):
                    missing_c = [c for c in cols.values() if c not in row]
                    if missing_c:
                        raise ValueError(f"real unit {uid!r}: missing column(s) {missing_c} for attribute {aname!r}")
                    val = {k: float(row[c]) for k, c in cols.items()}
                elif isinstance(cols, (list, tuple)):
                    missing_c = [c for c in cols if c not in row]
                    if missing_c:
                        raise ValueError(f"real unit {uid!r}: missing column(s) {missing_c} for attribute {aname!r}")
                    val = [float(row[c]) for c in cols]
                else:
                    raise ValueError(f"real unit {uid!r}: invalid columns binding for attribute {aname!r}")
            elif aname in row:
                val = _coerce_attr(row[aname], aspec)
            elif isinstance(ab.get(aname), Mapping) and "default" in ab[aname]:
                val = copy.deepcopy(ab[aname]["default"])
            elif cal_attr and aname == cal_attr:
                # Calibrated per-unit attribute: calibration supplies per-unit values rather than a column binding.
                rng = aspec.get("range") if "range" in aspec else aspec.get("test_range")
                if isinstance(cal_range, (list, tuple)) and len(cal_range) == 2:
                    mid = 0.5 * (float(cal_range[0]) + float(cal_range[1]))
                    val = math.exp(mid) if cal_scale == "log" else mid
                elif isinstance(rng, (list, tuple)) and len(rng) == 2:
                    val = 0.5 * (float(rng[0]) + float(rng[1]))
                else:
                    raise ValueError(f"real unit {uid!r}: calibrated attribute {aname!r} has no declared range")
                if isinstance(rng, (list, tuple)) and len(rng) == 2:
                    val = max(float(rng[0]), min(float(rng[1]), float(val)))
            else:
                raise ValueError(f"real unit {uid!r}: missing required attribute {aname!r} (no binding or default)")
            attrs[aname] = val
        units.append(UnitSpec(uid, attrs))

    if len(units) > max_units:
        # Include every unit at an attribute extreme + stratified remainder up to max_units
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

    # Packages from options.engine_map + options.packages (respecting option groups & never exploding Cartesian products)
    opts = spec_raw.get("options") if isinstance(spec_raw, Mapping) else {}
    emap = (opts.get("engine_map") or {}) if isinstance(opts, Mapping) else {}
    opt_comps = (opts.get("components") or {}) if isinstance(opts, Mapping) else {}
    pks_decl = (opts.get("packages") or {}) if isinstance(opts, Mapping) else {}
    absent_pkg = {c: cs["absent"] for c, cs in comp_specs.items()}
    packages: list[Package] = []

    def _pkg_from_opt_set(opt_names: tuple[str, ...]) -> Package:
        comp_map = dict(absent_pkg)
        for m in opt_names:
            if m in emap and isinstance(emap[m], Mapping):
                comp_map.update(emap[m])
        pid = "+".join(opt_names) if opt_names else "none"
        return Package(pid, comp_map)

    if isinstance(pks_decl, Mapping) and pks_decl and not pks_decl.get("generate"):
        items = list(pks_decl.items())
        if len(items) > max_packages:
            idx = sorted(set([0, len(items) - 1]
                             + rng.choice(len(items), size=max(0, max_packages - 2), replace=False).tolist()))
            items = [items[i] for i in idx]
        for pid, members in items:
            comp_map = dict(absent_pkg)
            for m in (members or []):
                if m in emap and isinstance(emap[m], Mapping):
                    comp_map.update(emap[m])
            packages.append(Package(str(pid), comp_map))
    elif isinstance(opt_comps, Mapping) and opt_comps and isinstance(emap, Mapping) and emap:
        # Respect mutually exclusive option groups (same grouping rule as toolkit.alloc.costs.packages)
        groups: dict[str, list[str]] = {}
        for oc, od in opt_comps.items():
            grp = str((od or {}).get("group") or oc) if isinstance(od, Mapping) else str(oc)
            groups.setdefault(grp, []).append(str(oc))
        group_choices = [[None] + sorted(vs) for _gk, vs in sorted(groups.items())]
        total_combos = math.prod(len(ch) for ch in group_choices)
        if total_combos <= max_packages:
            for pick in itertools.product(*group_choices):
                chosen_opts = tuple(sorted(c for c in pick if c is not None))
                packages.append(_pkg_from_opt_set(chosen_opts))
        else:
            seen_opts: set[tuple[str, ...]] = set()
            ordered_sets: list[tuple[str, ...]] = [()]
            seen_opts.add(())
            for oc in sorted(opt_comps):
                t = (str(oc),)
                if t not in seen_opts and len(ordered_sets) < max_packages:
                    seen_opts.add(t)
                    ordered_sets.append(t)
            # Sample additional valid multi-group packages without ever materializing full product
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
                packages.append(_pkg_from_opt_set(opt_tuple))
    else:
        # Engine-domain combinations without materializing huge Cartesian products
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

            # Include all-absent, all-max, each single-active value, then random draws
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


# --------------------------------------------------------------------------- Individual checks A1-A11
def check_a1(cand: BCAND.CandidateEngine, build_decl: Mapping, spec_raw: Mapping,
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

    # 2. describe() agrees with signed request on components, unit_attributes, outputs, age_groups
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


def check_a2(cand: BCAND.CandidateEngine, build_decl: Mapping, units: list[UnitSpec],
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


def check_a3(cand: BCAND.CandidateEngine, units: list[UnitSpec], packages: list[Package],
             horizon: Horizon, *, th: Mapping, build_decl: Mapping | None = None) -> dict:
    """A3 batch equals run: skipped when module has no `run_batch_state`; else exact pair-by-pair
    match across distinct parameter sets and packages (BE7)."""
    if not cand.has_batch():
        return {"id": "A3", "status": "skipped", "passed": 0, "total": 0, "problems": []}
    tol = float(th["batch_vs_run_rel_tol"])
    probs: list[str] = []
    # Pick distinct valid parameter sets so A3 catches batch code reusing member 0's params
    req = (build_decl or {}).get("request") or {}
    inert = set((req.get("inert") or {}).keys())
    ee = req.get("expected_effects") or {}
    param_specs = [p for p in (req.get("parameters") or []) if p.get("name") not in inert]
    # Prefer a parameter declared in expected_effects with multiple distinct domain points
    varied_name: str | None = None
    varied_pts: list = []
    for ps in sorted(param_specs, key=lambda p: (p["name"] not in ee, p["name"])):
        pts = _domain_points_param(ps)
        uniq = list(dict.fromkeys(pts))
        if len(uniq) >= 2:
            varied_name = str(ps["name"])
            varied_pts = uniq
            break
    if varied_name is None:
        for p in cand.parameters():
            if p.lo is not None and p.hi is not None and float(p.hi) > float(p.lo):
                lo, hi = float(p.lo), float(p.hi)
                varied_name = p.name
                varied_pts = [lo + 0.25 * (hi - lo), lo + 0.75 * (hi - lo)]
                break

    batch_pkgs = list(packages)
    if len(batch_pkgs) == 1 and varied_pts:
        batch_pkgs = [batch_pkgs[0], batch_pkgs[0]]
    p_sets: list[dict] = []
    for idx in range(len(batch_pkgs)):
        if varied_name is not None and varied_pts:
            p_sets.append({varied_name: varied_pts[idx % len(varied_pts)]})
        else:
            p_sets.append({})

    n_total, n_pass = 0, 0
    for u in units[:4]:
        b_outs = cand.run_batch(p_sets, u, batch_pkgs, horizon, seed=7)
        s_outs = cand.run_many([(prm, u, pk, horizon, 7) for prm, pk in zip(p_sets, batch_pkgs)])
        for prm, pk, bo, so in zip(p_sets, batch_pkgs, b_outs, s_outs):
            n_total += 1
            same, worst = _outcomes_close(bo, so, rel=tol, abs_tol=tol)
            if same:
                n_pass += 1
            else:
                probs.append(f"{u.id}/{pk.id} ({prm}): run_batch differs from run (rel={worst:.3g} > {tol})")
    return {"id": "A3", "status": "pass" if not probs else "fail", "passed": n_pass, "total": n_total,
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


def check_a4(cand: BCAND.CandidateEngine, build_decl: Mapping, spec_raw: Mapping,
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
        rhor = (Horizon(float(rhor_d["start"]), float(rhor_d["stop"]),
                        tuple(rhor_d.get("window") or (rhor_d["start"], rhor_d["stop"])),
                        {str(k): list(v) for k, v in (rhor_d.get("windows") or {}).items()})
                if isinstance(rhor_d, Mapping) else default_hor)
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


def check_a5(cand: BCAND.CandidateEngine, build_decl: Mapping, units: list[UnitSpec],
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


def check_a6_a7(cand: BCAND.CandidateEngine, build_decl: Mapping, spec_raw: Mapping,
                rng: np.random.Generator, *, n_bases: int, min_moving: int, th: Mapping,
                ref_units: list[UnitSpec] | None = None) -> tuple[dict, dict]:
    """A6 sensitivity wiring + param_reads + A7 declared directions (BE2, BE3, BE4, BE5)."""
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
    expected_effects = req.get("expected_effects") or {}
    absent_pkg = {c: cs["absent"] for c, cs in comp_specs.items()}
    hor = _default_horizon(spec_raw, req)

    # Map intervention decision inputs (BOTH parameter AND unit_attribute) to their owning component
    # so base points activate that component (evaluating across all active values up to 4 when a
    # component has multiple active modes so value ordering never hides an effect).
    input_to_comp: dict[tuple[str, str], tuple[str, list]] = {}
    for cname, spec_or_items in (req.get("decision_inputs") or {}).items():
        items = spec_or_items if isinstance(spec_or_items, list) else (spec_or_items.get("inputs") or [])
        active_vals = [v for v in (comp_specs.get(cname, {}).get("values") or []) if v != comp_specs.get(cname, {}).get("absent")]
        if active_vals:
            for it in items:
                if isinstance(it, Mapping) and it.get("kind") in ("parameter", "unit_attribute"):
                    k_norm = "attribute" if it["kind"] == "unit_attribute" else "parameter"
                    input_to_comp[(k_norm, str(it["name"]))] = (cname, list(active_vals[:4]))

    # Cost drivers are often zero under the all-absent package; map each declared cost driver to
    # active modes of components whose expected_effects include it so scale/unit attributes moving
    # cost drivers are evaluated under active intervention modes as well as the absent package.
    declared_cost_drivers = set((req.get("outputs") or {}).get("cost_drivers") or [])
    output_to_active_pkgs: dict[str, list[dict]] = {}
    for cname, cs in sorted(comp_specs.items()):
        c_eff = expected_effects.get(cname) or {}
        active_vals = [v for v in (cs.get("values") or []) if v != cs.get("absent")]
        if not active_vals:
            continue
        for oname in c_eff:
            lst = output_to_active_pkgs.setdefault(str(oname), [])
            for ov in active_vals[:4]:
                pkg_d = dict(absent_pkg, **{cname: ov})
                if pkg_d not in lst and len(lst) < 4:
                    lst.append(pkg_d)

    # Generate base units (interior of domains)
    base_units = generate_domain_units(req, n_bases, rng, ref_units=ref_units)
    a6_probs: list[str] = []
    a7_probs: list[str] = []
    wiring_rows: list[dict] = []
    outputs_moved: set[str] = set()
    reads_build_all: set[str] = set()
    reads_outcomes_all: set[str] = set()
    pair_total, pair_pass_a6, pair_pass_a7 = 0, 0, 0

    seeds = [31 + i for i in range(k_seeds)]

    # Build the complete A6/A7 evaluation plan across ALL inputs and run in a SINGLE worker batch
    all_cases: list[tuple[dict, UnitSpec, Package, Horizon, int]] = []
    input_plan: list[tuple[str, str, dict, list, int, int, int]] = []
    for iname, out_signs in sorted(expected_effects.items()):
        if iname in inert:
            continue
        if iname in param_specs:
            ikind = "parameter"
            pts = _domain_points_param(param_specs[iname])
        elif iname in comp_specs:
            ikind = "component"
            pts = list(comp_specs[iname]["values"])
        elif iname in attr_specs:
            ikind = "attribute"
            ref_v = base_units[0].attributes.get(iname) if base_units else None
            pts = _domain_points_attr(attr_specs[iname], ref_vec=ref_v)
        else:
            continue
        if (ikind, iname) in input_to_comp:
            oc, ovas = input_to_comp[(ikind, iname)]
            base_pkgs = [dict(absent_pkg, **{oc: ov}) for ov in ovas]
        elif ikind != "component" and any(o in declared_cost_drivers for o in out_signs):
            extra_pkgs: list[dict] = []
            for oname in sorted(out_signs):
                if oname in declared_cost_drivers:
                    for pk_d in output_to_active_pkgs.get(oname, []):
                        if pk_d not in extra_pkgs and len(extra_pkgs) < 4:
                            extra_pkgs.append(pk_d)
            if not extra_pkgs:
                for cname, cs in sorted(comp_specs.items()):
                    for ov in [v for v in (cs.get("values") or []) if v != cs.get("absent")][:4]:
                        pk_d = dict(absent_pkg, **{cname: ov})
                        if pk_d not in extra_pkgs and len(extra_pkgs) < 4:
                            extra_pkgs.append(pk_d)
            base_pkgs = [dict(absent_pkg), *extra_pkgs]
        else:
            base_pkgs = [dict(absent_pkg)]
        start_idx = len(all_cases)
        for base_pkg in base_pkgs:
            for bi, bu in enumerate(base_units):
                for pt in pts:
                    prm = {iname: float(pt)} if ikind == "parameter" else {}
                    pk_map = dict(base_pkg, **{iname: pt}) if ikind == "component" else dict(base_pkg)
                    u_use = UnitSpec(f"a6_{bi}", dict(bu.attributes, **{iname: pt})) if ikind == "attribute" else bu
                    for s in seeds:
                        all_cases.append((prm, u_use, Package(f"p_{bi}", pk_map), hor, s))
        input_plan.append((iname, ikind, dict(out_signs), pts, len(base_pkgs), start_idx, len(all_cases)))

    chunk_sz = 256 if cand.kind == "closed_form" else 32
    all_outs: list[Outcomes] = []
    for a in range(0, len(all_cases), chunk_sz):
        all_outs.extend(cand.run_many(all_cases[a:a + chunk_sz]))
    for o in all_outs:
        pr = (o.meta or {}).get("param_reads") or {}
        reads_build_all.update(pr.get("build") or [])
        reads_outcomes_all.update(pr.get("read_outcomes") or [])
    n_seeds = len(seeds)

    for iname, ikind, out_signs, pts, n_modes, start_idx, end_idx in input_plan:
        in_outs = all_outs[start_idx:end_idx]
        n_pts = len(pts)
        stride_mode = len(base_units) * n_pts * n_seeds
        ordered = all(isinstance(p, (int, float, bool)) for p in pts) and n_pts >= 2
        cat_abs_idx: int | None = None
        if ikind == "component" and not ordered and iname in comp_specs:
            abs_v = comp_specs[iname].get("absent")
            if abs_v in pts:
                cat_abs_idx = pts.index(abs_v)

        for oname, sign in sorted(out_signs.items()):
            pair_total += 1
            best_moved_bases = 0
            sign_ok_all = True
            max_rel_change = 0.0
            for mi in range(n_modes):
                mode_outs = in_outs[mi * stride_mode:(mi + 1) * stride_mode]
                moved_bases = 0
                mode_sign_ok = True
                for bi in range(len(base_units)):
                    ys_by_pt: list[list[float]] = []
                    for pi in range(n_pts):
                        off = (bi * n_pts + pi) * n_seeds
                        ys_by_pt.append([_extract_output(o, oname) for o in mode_outs[off:off + n_seeds]])
                    if cat_abs_idx is not None:
                        # Unordered categorical component: compare every active value against `absent`
                        # rather than arbitrary list endpoints or step monotonicity across list order.
                        y_abs = ys_by_pt[cat_abs_idx]
                        y0_mean = float(np.mean(y_abs))
                        floor = max(abs_floor, rel_floor * abs(y0_mean))
                        any_pos = False
                        any_neg = False
                        for pi in range(n_pts):
                            if pi == cat_abs_idx:
                                continue
                            d_pi = [ys_by_pt[pi][s] - y_abs[s] for s in range(len(seeds))]
                            m_pi = float(np.mean(d_pi))
                            rel_ch = abs(m_pi) / max(abs(y0_mean), 1e-12)
                            max_rel_change = max(max_rel_change, rel_ch)
                            if stochastic and len(seeds) > 1:
                                se_pi = float(np.std(d_pi, ddof=1)) / math.sqrt(len(seeds))
                                lo_pi, hi_pi = m_pi - z_crit * se_pi, m_pi + z_crit * se_pi
                                if lo_pi > floor:
                                    any_pos = True
                                if hi_pi < -floor:
                                    any_neg = True
                            else:
                                if m_pi > floor:
                                    any_pos = True
                                if m_pi < -floor:
                                    any_neg = True
                        span = max(float(np.mean(ys)) for ys in ys_by_pt) - min(float(np.mean(ys)) for ys in ys_by_pt)
                        moved = (span > floor) if sign == "nonzero" else (any_pos or any_neg)
                        if moved:
                            moved_bases += 1
                            outputs_moved.add(oname)
                            if sign == "+" and not (any_pos and not any_neg):
                                mode_sign_ok = False
                            elif sign == "-" and not (any_neg and not any_pos):
                                mode_sign_ok = False
                        continue
                    # Compare endpoints (pt[-1] vs pt[0]) and check monotonicity across ALL adjacent ordered steps
                    diffs = [ys_by_pt[-1][s] - ys_by_pt[0][s] for s in range(len(seeds))]
                    y0_mean = float(np.mean(ys_by_pt[0]))
                    floor = max(abs_floor, rel_floor * abs(y0_mean))
                    d_mean = float(np.mean(diffs))
                    rel_ch = abs(d_mean) / max(abs(y0_mean), 1e-12)
                    max_rel_change = max(max_rel_change, rel_ch)
                    if stochastic and len(seeds) > 1:
                        se = float(np.std(diffs, ddof=1)) / math.sqrt(len(seeds))
                        lo_ci, hi_ci = d_mean - z_crit * se, d_mean + z_crit * se
                        moved = (lo_ci > floor) or (hi_ci < -floor)
                    else:
                        span = max(float(np.mean(ys)) for ys in ys_by_pt) - min(float(np.mean(ys)) for ys in ys_by_pt)
                        moved = (span > floor) if sign == "nonzero" else (abs(d_mean) > floor)
                    if moved:
                        moved_bases += 1
                        outputs_moved.add(oname)
                        if sign == "+":
                            if not (d_mean > 0):
                                mode_sign_ok = False
                            elif ordered:
                                for j in range(len(ys_by_pt) - 1):
                                    s_diffs = [ys_by_pt[j + 1][s] - ys_by_pt[j][s] for s in range(len(seeds))]
                                    s_mean = float(np.mean(s_diffs))
                                    if stochastic and len(seeds) > 1:
                                        s_se = float(np.std(s_diffs, ddof=1)) / math.sqrt(len(seeds))
                                        if s_mean + z_crit * s_se < -floor:
                                            mode_sign_ok = False
                                    elif s_mean < -floor:
                                        mode_sign_ok = False
                        elif sign == "-":
                            if not (d_mean < 0):
                                mode_sign_ok = False
                            elif ordered:
                                for j in range(len(ys_by_pt) - 1):
                                    s_diffs = [ys_by_pt[j + 1][s] - ys_by_pt[j][s] for s in range(len(seeds))]
                                    s_mean = float(np.mean(s_diffs))
                                    if stochastic and len(seeds) > 1:
                                        s_se = float(np.std(s_diffs, ddof=1)) / math.sqrt(len(seeds))
                                        if s_mean - z_crit * s_se > floor:
                                            mode_sign_ok = False
                                    elif s_mean > floor:
                                        mode_sign_ok = False
                if moved_bases > best_moved_bases:
                    best_moved_bases = moved_bases
                if moved_bases > 0 and not mode_sign_ok:
                    sign_ok_all = False
            moved_bases = best_moved_bases
            ok_a6 = moved_bases >= min(min_moving, n_bases)
            ok_a7 = ok_a6 and sign_ok_all  # A7 CANNOT pass vacuously if A6 failed (BE4)
            wiring_rows.append({
                "input": iname, "output": oname, "sign": sign,
                "moved_bases": moved_bases, "n_bases": n_bases,
                "max_rel_change": round(max_rel_change, 6),
                "a6_ok": ok_a6, "a7_ok": ok_a7,
            })
            if ok_a6:
                pair_pass_a6 += 1
            else:
                a6_probs.append(f"({iname} -> {oname}): moved at only {moved_bases}/{n_bases} base points "
                                f"(max_rel_change={max_rel_change:.3g}, floor={rel_floor})")
            if ok_a7:
                pair_pass_a7 += 1
            elif ok_a6 and not sign_ok_all:
                a7_probs.append(f"({iname} -> {oname}): declared sign {sign!r} violated")
            else:
                a7_probs.append(f"({iname} -> {oname}): cannot verify sign {sign!r} because pair did not move in A6")

    # Every declared non-inert parameter must be read; read_outcomes-only parameters require parameter_roles (BE5)
    for pname in sorted(param_specs):
        if pname in inert:
            continue
        if pname not in reads_build_all and pname not in reads_outcomes_all:
            a6_probs.append(f"parameter {pname!r} was never read during A6 evaluations (dead parameter)")
        elif pname in reads_outcomes_all and pname not in reads_build_all and pname not in param_roles:
            a6_probs.append(f"parameter {pname!r} was read ONLY in read_outcomes() without a signed parameter_roles entry")

    # Every declared output must be moved by at least one input (accepting both bare names and full paths)
    req_outs = req.get("outputs") or {}
    for pc in (req_outs.get("per_capita") or []):
        if not (pc in outputs_moved or f"per_capita_all.{pc}" in outputs_moved
                or any(o.startswith(f"per_capita.{pc}.") for o in outputs_moved)):
            a6_probs.append(f"declared output {pc!r} was not moved by any input in A6")
    for drv in (req_outs.get("cost_drivers") or []):
        if not (drv in outputs_moved or f"cost.{drv}" in outputs_moved):
            a6_probs.append(f"declared output {drv!r} was not moved by any input in A6")
    for wname, metrics in (req_outs.get("windows") or {}).items():
        for m in (metrics or []):
            if not (m in outputs_moved or f"windows.{wname}.{m}" in outputs_moved):
                a6_probs.append(f"declared output 'windows.{wname}.{m}' was not moved by any input in A6")

    a6 = {"id": "A6", "status": "pass" if not a6_probs else "fail",
          "passed": pair_pass_a6, "total": pair_total, "problems": a6_probs[:12], "wiring": wiring_rows}
    a7 = {"id": "A7", "status": "pass" if not a7_probs else "fail",
          "passed": pair_pass_a7, "total": pair_total, "problems": a7_probs[:12]}
    return a6, a7


def build_trial_bench_artifacts(cand: BCAND.CandidateEngine, build_decl: Mapping, spec_raw: Mapping) -> tuple[dict, dict, dict, dict | None]:
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
            bm_entry["target"] = round(0.5 * (lo + hi), 6)
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
        "engine": BCAND.CandidateEngine.ENGINE_REF,
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
    cov_doc = {
        "engine": cand.describe().name,
        "absent_values": {c: [cs["absent"]] for c, cs in comp_specs.items()},
        "benchmarks": cov_benchmarks,
    }

    # If the built engine declares any `fitted` parameters, create a stamped fitted.yaml backing them
    fitted_params = [p for p in cand.parameters() if p.kind == "fitted"]
    fitted_doc = None
    if fitted_params:
        fitted_doc = BM.stamp({
            "parameters": {p.name: {"value": p.value, "lo": p.lo, "hi": p.hi} for p in fitted_params},
            "band_declarations": BS.fit_declarations(pack_doc),
        }, cand)

    # Run pack through benchmark.Runner(mode='fit' when fitted_doc not yet written on disk, then stamp)
    runner = BM.Runner(pack_doc, cand, workers=1, seeds=seeds, mode="fit")
    raw_res = BM.run_pack(pack_doc, cand, runner, {})
    res_doc = BM.stamp(raw_res, cand)
    return pack_doc, res_doc, cov_doc, fitted_doc


def check_a8(cand: BCAND.CandidateEngine, build_decl: Mapping, spec_raw: Mapping,
             *, departures_status: str = "signed", base_dir: str | None = None) -> tuple[dict, dict, dict, dict, dict | None]:
    """A8 trial benchmarks + built coverage policy (`policy='built'`, BE15)."""
    pack_doc, res_doc, cov_doc, fitted_doc = build_trial_bench_artifacts(cand, build_decl, spec_raw)
    rows = res_doc.get("benchmarks") or []
    card = cand.describe()
    pnames = [p.name for p in cand.parameters()]

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


def check_a9(cand: BCAND.CandidateEngine, build_decl: Mapping, spec_raw: Mapping,
             dossier_benchmarks: list[dict] | None = None) -> dict:
    """A9 literature/dossier value benchmarks: reported, gating for every ID in `gating_benchmarks`
    (a declared gating benchmark that is missing/unevaluated fails A9)."""
    req = build_decl["request"]
    gating_ids = {str(g) for g in (req.get("gating_benchmarks") or [])}
    items = list(dossier_benchmarks or [])
    evaluated_ids = {str(b["id"]) for b in items if isinstance(b, Mapping) and b.get("id") is not None}
    missing_gating = sorted(gating_ids - evaluated_ids)
    probs: list[str] = [f"gating literature benchmark {gid!r} is missing (not evaluated)" for gid in missing_gating]
    if not items:
        return {"id": "A9", "status": "pass" if not probs else "fail", "passed": 0,
                "total": len(missing_gating), "problems": probs, "rows": []}
    hor = _default_horizon(spec_raw, req)
    absent_pkg = {c: cs["absent"] for c, cs in (req.get("components") or {}).items()}
    rows: list[dict] = []
    n_pass = 0
    for b in items:
        bid = str(b["id"])
        u = UnitSpec(f"a9_{bid}", dict(b.get("unit") or {}))
        pk = Package(f"a9_{bid}", dict(absent_pkg, **(b.get("package") or {})))
        out = cand.run(dict(b.get("params") or {}), u, pk, hor, seed=41)
        val = _extract_output(out, str(b["path"]))
        lo, hi = float(b["band"][0]), float(b["band"][1])
        in_b = lo <= val <= hi
        if in_b:
            n_pass += 1
        elif bid in gating_ids:
            probs.append(f"gating literature benchmark {bid}: computed={val:.5g} outside [{lo}, {hi}]")
        rows.append({"id": bid, "computed": val, "band": [lo, hi], "in_band": in_b, "gating": bid in gating_ids})
    return {"id": "A9", "status": "pass" if not probs else "fail", "passed": n_pass,
            "total": len(items) + len(missing_gating), "problems": probs, "rows": rows}


def check_a10(cand: BCAND.CandidateEngine, build_decl: Mapping, spec_raw: Mapping,
              *, base_dir: str | None = None) -> dict:
    """A10 identifiability on calibration targets via `calibrators.calibrate_as_run` (non-gating on its own)."""
    cal = spec_raw.get("calibration") if isinstance(spec_raw, Mapping) else None
    method = str((cal or {}).get("method", "none")) if isinstance(cal, Mapping) else "none"
    if method == "none" and not (isinstance(cal, Mapping) and cal.get("joint")):
        return {"id": "A10", "status": "skipped", "passed": 0, "total": 0, "problems": [], "classes": {}}
    from toolkit.engines.built import calibrators as BCAL
    try:
        res = BCAL.calibrate_as_run(
            lambda _cfg=None: cand,
            {"spec_raw": dict(spec_raw), "base_dir": base_dir or os.getcwd()},
            seed=53,
        )
        classes = dict(res.get("classes") or {})
        lik_classes = dict(res.get("likelihood_classes") or {})
        ridges = list(res.get("ridges") or [])
        ridge_params = {str(p) for r in ridges if isinstance(r, Mapping) for p in (r.get("params") or [])}
        all_names = set(classes) | set(lik_classes) | ridge_params
        unid = sorted(
            k for k in all_names
            if classes.get(k) in ("unidentified", "prior_dominated")
            or lik_classes.get(k) in ("on_ridge", "unidentified")
            or k in ridge_params
        )
        probs: list[str] = []
        if res.get("blocked"):
            probs.append(f"joint calibration blocked (unmatched={res.get('unmatched') or []})")
        return {
            "id": "A10",
            "status": "pass" if not res.get("blocked") else "fail",
            "passed": max(0, len(all_names) - len(unid)),
            "total": len(all_names),
            "classes": classes,
            "likelihood_classes": lik_classes,
            "ridges": ridges,
            "unidentified": unid,
            "problems": probs,
        }
    except Exception as e:  # noqa: BLE001 -- A10 does not gate admission on its own (open question 8)
        return {"id": "A10", "status": "unavailable", "passed": 0, "total": 0,
                "classes": {}, "problems": [f"A10 calibration check unavailable: {e}"]}


def check_a11(cand: BCAND.CandidateEngine, ref_cand: BCAND.CandidateEngine | None, build_decl: Mapping,
              cases: list[tuple[dict, UnitSpec, Package, Horizon]], *, th: Mapping, required: bool = True) -> dict:
    """A11 independent reference equivalence test (BE17):
    - Compares every individual full path (`per_capita.<o>.<g>`, `windows.<w>.<m>`, `cost.<d>`).
    - Deterministic: every compared output within `min(request.reference_tolerance, thresholds.reference_rel_ceiling)`.
    - Stochastic: 95% paired CRN CI of mean difference must lie ENTIRELY inside tolerance; doubles K up to
      `stochastic_seeds_max`; still straddling -> `inconclusive` (NOT admitted!)."""
    if ref_cand is None:
        if not required:
            return {"id": "A11", "status": "skipped", "passed": 0, "total": 0, "problems": []}
        return {"id": "A11", "status": "unavailable", "passed": 0, "total": 0,
                "problems": ["independent reference implementation unavailable (A11 required)"]}
    req = build_decl["request"]
    stochastic = bool(req.get("stochastic", False))
    rtol_decl = req.get("reference_tolerance") or {}
    rel_lim = min(float(rtol_decl.get("rel", th["reference_rel_ceiling"])), float(th["reference_rel_ceiling"]))
    abs_lim = float(rtol_decl.get("abs", th["reference_abs_default"]))
    z_crit = NormalDist().inv_cdf(0.5 + float(th["move_ci_level"]) / 2.0)
    k_init = int(th["stochastic_seeds_init"])
    k_max = int(th["stochastic_seeds_max"])

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
    inconclusive = False
    n_total, n_pass = 0, 0
    rows: list[dict] = []

    if not stochastic:
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
    else:
        for ci, (prm, u, pk, hor) in enumerate(cases):
            # Stochastic equivalence: double K until CI lies inside [-tol, +tol], clearly outside, or hits k_max
            k = k_init
            while True:
                seeds = [200 + ci * 1000 + s for s in range(k)]
                c_outs = cand.run_many([(prm, u, pk, hor, s) for s in seeds])
                r_outs = ref_cand.run_many([(prm, u, pk, hor, s) for s in seeds])
                need_more = False
                case_rows = []
                case_probs = []
                case_inc = False
                for oname in out_names:
                    cs = [_extract_output(o, oname) for o in c_outs]
                    rs = [_extract_output(o, oname) for o in r_outs]
                    diffs = [a - b for a, b in zip(cs, rs)]
                    mean_d = float(np.mean(diffs))
                    mean_r = float(np.mean(rs))
                    se = float(np.std(diffs, ddof=1)) / math.sqrt(k) if k > 1 else 0.0
                    lo_ci, hi_ci = mean_d - z_crit * se, mean_d + z_crit * se
                    tol = max(abs_lim, rel_lim * max(abs(mean_r), 1e-12))
                    if lo_ci >= -tol and hi_ci <= tol:
                        case_rows.append({"case": ci, "output": oname, "mean_diff": mean_d, "ci": [lo_ci, hi_ci],
                                          "tol": tol, "k": k, "status": "equivalent"})
                    elif lo_ci > tol or hi_ci < -tol:
                        case_probs.append(f"case {ci} {oname}: ref mismatch CI [{lo_ci:.4g}, {hi_ci:.4g}] outside +/-{tol:.4g} (K={k})")
                        case_rows.append({"case": ci, "output": oname, "mean_diff": mean_d, "ci": [lo_ci, hi_ci],
                                          "tol": tol, "k": k, "status": "fail"})
                    elif k < k_max:
                        need_more = True
                        break
                    else:
                        case_inc = True
                        case_probs.append(f"case {ci} {oname}: inconclusive at K={k} (CI [{lo_ci:.4g}, {hi_ci:.4g}], tol +/-{tol:.4g})")
                        case_rows.append({"case": ci, "output": oname, "mean_diff": mean_d, "ci": [lo_ci, hi_ci],
                                          "tol": tol, "k": k, "status": "inconclusive"})
                if need_more:
                    k = min(k * 2, k_max)
                    continue
                for cr in case_rows:
                    n_total += 1
                    if cr["status"] == "equivalent":
                        n_pass += 1
                rows.extend(case_rows)
                probs.extend(case_probs)
                if case_inc:
                    inconclusive = True
                break

    if probs:
        status = "inconclusive" if inconclusive and not any("ref mismatch" in p for p in probs) else "fail"
    else:
        status = "pass"
    comp_sha = _sha256_json(rows)
    return {"id": "A11", "status": status, "passed": n_pass, "total": n_total,
            "problems": probs[:12], "rows": rows[:12], "comparison_sha256": comp_sha}


# --------------------------------------------------------------------------- Public checks (try tool & overfit baseline)
def run_public_checks(cand: BCAND.CandidateEngine, build_decl: Mapping, spec_raw: Mapping,
                      *, checks: list[str] | None = None) -> dict:
    """Run ONLY public checks using `PUBLIC_SEED_BYTES` (shared by `try_tool` and overfit comparison)."""
    th = load_thresholds()
    req = build_decl["request"]
    want = set(checks) if checks is not None else {"A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8"}
    rng = _rng_from(PUBLIC_SEED_BYTES, "public")
    hor = _default_horizon(spec_raw, req)
    comp_specs = req.get("components") or {}
    absent_pkg = {c: cs["absent"] for c, cs in comp_specs.items()}
    pub_pkgs = [Package("pub_none", dict(absent_pkg))]
    for c, cs in sorted(comp_specs.items()):
        for v in cs["values"]:
            if v != cs["absent"]:
                pub_pkgs.append(Package(f"pub_{c}_{v}", dict(absent_pkg, **{c: v})))
    pub_units = generate_domain_units(req, 2, rng)
    out_checks: dict[str, dict] = {}
    if "A1" in want:
        out_checks["A1"] = check_a1(cand, build_decl, spec_raw, pub_units, pub_pkgs, hor)
    if "A2" in want:
        out_checks["A2"] = check_a2(cand, build_decl, pub_units, pub_pkgs, hor, th=th, check_invariance=False)
    if "A3" in want:
        out_checks["A3"] = check_a3(cand, pub_units, pub_pkgs, hor, th=th, build_decl=build_decl)
    if "A4" in want:
        out_checks["A4"] = check_a4(cand, build_decl, spec_raw, _rng_from(PUBLIC_SEED_BYTES, "a4"), n_free=1, th=th)
    if "A5" in want:
        out_checks["A5"] = check_a5(cand, build_decl, pub_units, pub_pkgs, hor, th=th)
    if "A6" in want or "A7" in want:
        a6, a7 = check_a6_a7(cand, build_decl, spec_raw, _rng_from(PUBLIC_SEED_BYTES, "a6"),
                             n_bases=1, min_moving=1, th=th)
        if "A6" in want:
            out_checks["A6"] = a6
        if "A7" in want:
            out_checks["A7"] = a7
    if "A8" in want:
        a8, *_ = check_a8(cand, build_decl, spec_raw)
        out_checks["A8"] = a8
    ok_all = all(c.get("status") in ("pass", "skipped", "not_applicable") for c in out_checks.values())
    return {"ok": ok_all, "checks": out_checks}


# --------------------------------------------------------------------------- Reuse key & cross-run reuse (§5.4, BE32)
def compute_reuse_key(
    snapshot_sha256: str,
    build_decl: Mapping,
    spec_raw: Mapping,
    *,
    config: Mapping | None = None,
    require_reference: bool = True,
    reference_files: Mapping[str, bytes] | None = None,
    dossier_bytes: bytes | None = None,
    dossier_benchmarks: list[dict] | None = None,
    base_dir: str | None = None,
    deps_digest: str | None = None,
) -> str:
    """Reuse key over every resolved input of every admission check (BE32)."""
    def _file_sha(p: str) -> str:
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                return _sha256_bytes(fh.read())
        return "missing"

    def _collect_refs(obj, prefix: str, out: dict[str, str]) -> None:
        if isinstance(obj, Mapping):
            for k, v in sorted(obj.items(), key=lambda kv: str(kv[0])):
                ks = str(k)
                if ks in ("csv", "targets_csv", "file", "data", "path") and isinstance(v, str) and v:
                    out[f"{prefix}.{ks}:{v}"] = _file_sha(os.path.join(base_dir, v))
                else:
                    _collect_refs(v, f"{prefix}.{ks}", out)
        elif isinstance(obj, list):
            for idx, item in enumerate(obj):
                _collect_refs(item, f"{prefix}[{idx}]", out)

    du = spec_raw.get("decision_units") if isinstance(spec_raw, Mapping) else None
    cal = spec_raw.get("calibration") if isinstance(spec_raw, Mapping) else None
    data_digests: dict[str, str] = {}
    if base_dir:
        _collect_refs(du, "units", data_digests)
        _collect_refs(cal, "cal", data_digests)
    ref_sha = (
        _sha256_json({str(k): _sha256_bytes(bytes(v)) for k, v in sorted(reference_files.items())})
        if reference_files is not None else "none"
    )
    payload = {
        "snapshot_sha256": str(snapshot_sha256),
        "request_sha256": BREQ.request_sha256(build_decl),
        "config_sha256": BADAPT.effective_config_sha256(config or {}),
        "horizon": spec_raw.get("horizon") if isinstance(spec_raw, Mapping) else None,
        "decision_units": du,
        "options": spec_raw.get("options") if isinstance(spec_raw, Mapping) else None,
        "objective": spec_raw.get("objective") if isinstance(spec_raw, Mapping) else None,
        "calibration": cal,
        "engine_departures": spec_raw.get("engine_departures") if isinstance(spec_raw, Mapping) else None,
        "data_digests": data_digests,
        "require_reference": bool(require_reference),
        "reference_sha256": ref_sha,
        "dossier_sha256": _sha256_bytes(dossier_bytes) if dossier_bytes is not None else "none",
        "dossier_benchmarks": dossier_benchmarks,
        "thresholds_sha256": _file_sha(THRESHOLDS_PATH),
        "oracles_sha256": _file_sha(os.path.join(HERE, "oracles.py")),
        "admission_sha256": _file_sha(os.path.abspath(__file__)),
        "deps_sha256": deps_digest if deps_digest is not None else BADAPT.deps_sha256(),
    }
    return _sha256_json(payload)


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


def try_reuse_admission(prior_run_dir: str, target_run_dir: str, expected_reuse_key: str) -> dict | None:
    """Reuse a prior run's admission when `reuse_key` matches, the prior run is sandboxed, and
    every mandatory snapshot, optional reference snapshot, and bench artifact verifies (BE32). Never
    copies `signoff.json` (the owner signs the engine again for the new run, §5.4). Idempotent over
    existing immutable target snapshots."""
    # Refuse reuse of any unsandboxed development run so unsandboxed.json can never be washed away
    if os.path.lexists(os.path.join(prior_run_dir, "engine_build", "unsandboxed.json")):
        return None
    if BWORK.unsandboxed_findings(prior_run_dir):
        return None
    try:
        adm_bytes, st = SIO.read_nofollow(prior_run_dir, "engine_build/admission.json")
        if st.st_nlink > 1:
            return None
        adm = json.loads(adm_bytes.decode("utf-8"))
    except (FileNotFoundError, SIO.LinkRefused, OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(adm, dict) or adm.get("status") != "admitted" or adm.get("reuse_key") != expected_reuse_key:
        return None
    snap_sha = adm.get("snapshot_sha256")
    if not _is_sha64(snap_sha):
        return None
    prior_snaps = os.path.join(prior_run_dir, "engine_build", "snapshots")
    try:
        verified_files = BSNAP.read_verified_snapshot(prior_snaps, snap_sha)
    except BSNAP.SnapshotError:
        return None
    # Require EVERY mandatory bench/* digest in prior admission.json and verify every file before copying
    digests = adm.get("digests") if isinstance(adm.get("digests"), Mapping) else {}
    for req_rel in BADAPT.BENCH_FILES:
        if not _is_sha64(digests.get(req_rel)):
            return None
    if os.path.lexists(os.path.join(prior_run_dir, "engine_build", "bench", "fitted.yaml")) and "bench/fitted.yaml" not in digests:
        return None
    verified_ref_files: dict[str, bytes] | None = None
    ref_sha: str | None = None
    if "reference_snapshot" in digests:
        ref_sha = digests.get("reference_snapshot")
        if not _is_sha64(ref_sha):
            return None
        prior_ref_snaps = os.path.join(prior_run_dir, "engine_build", "reference", "snapshots")
        try:
            verified_ref_files = BSNAP.read_verified_snapshot(prior_ref_snaps, ref_sha)
        except BSNAP.SnapshotError:
            return None
    if "reference_comparison" in digests and not _is_sha64(digests.get("reference_comparison")):
        return None
    copied_bench: dict[str, bytes] = {}
    for rel, want in digests.items():
        if rel in ("reference_snapshot", "reference_comparison"):
            continue
        if not _is_sha64(want):
            return None
        try:
            raw, bst = SIO.read_nofollow(prior_run_dir, f"engine_build/{rel}")
            if bst.st_nlink > 1 or _sha256_bytes(raw) != want:
                return None
            copied_bench[rel] = raw
        except (FileNotFoundError, SIO.LinkRefused, OSError):
            return None

    # Publish verified candidate (and optional reference) snapshots idempotently
    _publish_verified_snapshot(os.path.join(target_run_dir, "engine_build", "snapshots"), snap_sha, verified_files)
    if verified_ref_files is not None and ref_sha is not None:
        _publish_verified_snapshot(
            os.path.join(target_run_dir, "engine_build", "reference", "snapshots"),
            ref_sha,
            verified_ref_files,
        )

    if "bench/fitted.yaml" not in copied_bench:
        SIO.remove_nofollow(target_run_dir, "engine_build/bench/fitted.yaml", missing_ok=True)
    for rel, raw in copied_bench.items():
        SIO.write_nofollow(target_run_dir, f"engine_build/{rel}", raw, atomic=True)
    prior_sheet = ""
    try:
        prior_sheet = SIO.read_nofollow(prior_run_dir, "engine_build/admission_sheet.md")[0].decode("utf-8")
    except (FileNotFoundError, SIO.LinkRefused, OSError, UnicodeDecodeError):
        pass
    banner = f"> **Reused from run `{os.path.abspath(prior_run_dir)}`** (reuse key `{expected_reuse_key[:16]}`; requires new sign-off).\n\n"
    SIO.write_nofollow(target_run_dir, "engine_build/admission_sheet.md", (banner + prior_sheet).encode("utf-8"), atomic=True)
    new_adm = dict(adm, reused_from=os.path.abspath(prior_run_dir))
    SIO.write_nofollow(target_run_dir, "engine_build/admission.json",
                       json.dumps(new_adm, indent=1, sort_keys=True).encode("utf-8"), atomic=True)
    return new_adm


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
        f"- **Reuse key:** `{adm.get('reuse_key')}`",
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
    if adm.get("overfit_suspected"):
        lines += ["", "> **HIGH (`engine_overfit_suspected`):** Public checks passed while hidden checks failed."]
    a6 = (adm.get("checks") or {}).get("A6") or {}
    if a6.get("wiring"):
        lines += ["", "## Sensitivity wiring (A6 / A7)", "| Input | Output | Sign | Moved | Max rel change | A6 | A7 |", "|---|---|---|---|---|---|---|"]
        for r in a6["wiring"]:
            lines.append(f"| {r['input']} | {r['output']} | {r['sign']} | {r['moved_bases']}/{r['n_bases']} | "
                         f"{r['max_rel_change']} | {r['a6_ok']} | {r['a7_ok']} |")
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
    require_reference: bool = True,
    dossier_bytes: bytes | None = None,
    dossier_benchmarks: list[dict] | None = None,
    departures_status: str = "signed",
    unsandboxed_ok: bool = False,
    run_public_baseline: bool = True,
    _force_sandbox: str | None = "auto",
) -> dict:
    """Run the full admission gate (S1-S4, A1-A11) on an immutable snapshot in `run_dir`,
    writing `engine_build/bench/*`, `engine_build/admission_sheet.md`, and atomically
    `engine_build/admission.json` last."""
    import yaml
    th = load_thresholds()
    snaps_dir = os.path.join(run_dir, "engine_build", "snapshots")
    verified_files = BSNAP.read_verified_snapshot(snaps_dir, snapshot_sha256)
    seed_bytes, commitment = _load_or_commit_hidden_seed(run_dir)
    reuse_key = compute_reuse_key(
        snapshot_sha256, build_decl, spec_raw,
        config=config,
        require_reference=require_reference,
        reference_files=reference_files,
        dossier_bytes=dossier_bytes,
        dossier_benchmarks=dossier_benchmarks,
        base_dir=run_dir,
    )

    # S1-S4 static checks BEFORE any run
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
            "reuse_key": reuse_key,
            "static_problems": static_probs,
            "checks": {},
            "digests": {},
            "overfit_suspected": False,
            "feedback": {"static_problems": static_probs},
        }
        SIO.write_nofollow(run_dir, "engine_build/admission_sheet.md", render_admission_sheet(adm).encode("utf-8"), atomic=True)
        SIO.write_nofollow(run_dir, "engine_build/admission.json",
                           json.dumps(adm, indent=1, sort_keys=True).encode("utf-8"), atomic=True)
        return adm

    cand = BCAND.CandidateEngine(
        verified_files, build_decl, config=config, run_dir=run_dir,
        unsandboxed_ok=unsandboxed_ok, id_salt=commitment[:16],
    )
    cand._force_sandbox = _force_sandbox

    ref_cand = None
    if reference_files is not None:
        ref_build = copy.deepcopy(dict(build_decl))
        ref_build["kind"] = "closed_form"
        ref_cand = BCAND.CandidateEngine(
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

    # Optional public baseline: gates admission when enabled AND drives overfit detection
    public_ok = False
    public_failed: list[str] = []
    public_checks: dict = {}
    if run_public_baseline:
        try:
            pub_res = run_public_checks(cand, build_decl, spec_raw)
            public_ok = bool(pub_res["ok"])
            public_checks = pub_res.get("checks") or {}
            public_failed = sorted(
                k for k, v in public_checks.items()
                if v.get("status") not in ("pass", "skipped", "not_applicable")
            )
        except Exception as exc:  # noqa: BLE001
            public_ok = False
            public_failed = ["public_runtime_error"]
            public_checks = {"error": f"{type(exc).__name__}: {exc}"}

    req = build_decl["request"]
    hor = _default_horizon(spec_raw, req)
    rng_h = _rng_from(seed_bytes, "hidden")
    try:
        real_units, real_pkgs = _real_units_and_packages(
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
    checks["A3"] = _safe_check("A3", lambda: check_a3(cand, a1_units[:4], real_pkgs, hor, th=th, build_decl=build_decl))
    checks["A4"] = _safe_check("A4", lambda: check_a4(cand, build_decl, spec_raw, _rng_from(seed_bytes, "a4"),
                                                      n_free=int(th["a4_hidden_settings"]), th=th, ref_units=real_units))
    checks["A5"] = _safe_check("A5", lambda: check_a5(cand, build_decl, a5_units, real_pkgs, hor, th=th))
    try:
        a6, a7 = check_a6_a7(cand, build_decl, spec_raw, _rng_from(seed_bytes, "a6"),
                             n_bases=int(th["a6_base_points"]), min_moving=int(th["a6_min_moving_bases"]),
                             th=th, ref_units=real_units)
    except Exception as exc:  # noqa: BLE001
        err_msg = f"runtime error: {type(exc).__name__}: {exc}"
        a6 = {"id": "A6", "status": "fail", "passed": 0, "total": 1, "problems": [err_msg], "wiring": []}
        a7 = {"id": "A7", "status": "fail", "passed": 0, "total": 1, "problems": [err_msg]}
    checks["A6"] = a6
    checks["A7"] = a7

    pack_doc = res_doc = cov_doc = fitted_doc = None
    try:
        a8, pack_doc, res_doc, cov_doc, fitted_doc = check_a8(
            cand, build_decl, spec_raw, departures_status=departures_status, base_dir=run_dir,
        )
    except Exception as exc:  # noqa: BLE001
        a8 = {"id": "A8", "status": "fail", "passed": 0, "total": 1,
              "problems": [f"runtime error: {type(exc).__name__}: {exc}"], "rows": [], "routing": None}
    checks["A8"] = a8
    checks["A9"] = _safe_check("A9", lambda: check_a9(cand, build_decl, spec_raw, dossier_benchmarks=dossier_benchmarks))
    checks["A10"] = _safe_check("A10", lambda: check_a10(cand, build_decl, spec_raw, base_dir=run_dir))
    a11_cases = [(dict(), u, real_pkgs[i % len(real_pkgs)], hor) for i, u in enumerate((real_units + gen_units_a1)[:4])]
    checks["A11"] = _safe_check("A11", lambda: check_a11(cand, ref_cand, build_decl, a11_cases, th=th, required=require_reference))

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

    gating_ids = ("A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9", "A11")
    failed_gating = [cid for cid in gating_ids if checks[cid]["status"] not in ("pass", "skipped", "not_applicable")]
    if run_public_baseline and not public_ok:
        failed_gating.append("public")
    admitted = not failed_gating
    # Overfit finding fires ONLY when public baseline passed AND a hidden behavioral check actually executed & failed
    # (never when an external prerequisite such as A11's reference implementation was merely `unavailable`).
    hidden_behavioral_failed = [cid for cid in gating_ids if checks[cid]["status"] in ("fail", "inconclusive")]
    overfit = bool(run_public_baseline and public_ok and hidden_behavioral_failed)
    findings: list[dict] = []
    if overfit:
        findings.append({
            "kind": "engine_overfit_suspected",
            "severity": "HIGH",
            "stage": "ENGINE_ADMIT",
            "target_stage": "ENGINE",
            "claim": f"agent-built engine passed public checks but failed hidden check(s) {hidden_behavioral_failed}",
            "scope_declarable": False,
        })
    # Feedback for a hidden failure names ONLY check ids and output names (never hidden inputs/values, §4.6)
    feedback = {
        "failed_checks": failed_gating,
        "public_failed": public_failed,
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
        "reuse_key": reuse_key,
        "static_problems": [],
        "checks": checks,
        "public_ok": public_ok if run_public_baseline else None,
        "digests": digests,
        "overfit_suspected": overfit,
        "findings": findings,
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
                     require_reference: bool = True, run_public: bool = False,
                     with_ref: bool = True):
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
            unsandboxed_ok=dev_un, run_public_baseline=run_public,
        )
        return td, d, b, sp, man["snapshot_sha256"], adm

    # BE1: healthy two-group SIR Starsim fixture passes every check -> status: admitted
    td1, d1, b1, sp1, sha1, adm1 = _run_fixture(None, run_public=True)
    try:
        bad1 = {k: v.get("problems") for k, v in adm1["checks"].items() if v.get("status") not in ("pass", "skipped", "not_applicable")}
        ok(adm1["status"] == "admitted" and not bad1 and os.path.isfile(os.path.join(d1, "engine_build", "admission_sheet.md")),
           f"BE1 healthy two-group SIR Starsim fixture passes every check and is admitted (bad={bad1})")

        # BE32: reuse key hit copies admission & bench/ (idempotent over 0555 snapshot), refuses unsandboxed runs,
        # and invalidates on changed calibration targets, config, require_reference, or dep pin
        key1 = adm1["reuse_key"]
        sp_cal = copy.deepcopy(sp1)
        sp_cal["calibration"] = {"method": "likelihood", "targets": {"csv": "data/other_targets.csv"}}
        key_cal = compute_reuse_key(sha1, b1, sp_cal, base_dir=d1)
        key_dep = compute_reuse_key(sha1, b1, sp1, base_dir=d1, deps_digest="0" * 64)
        key_noref = compute_reuse_key(sha1, b1, sp1, base_dir=d1, require_reference=False)
        key_cfg = compute_reuse_key(sha1, b1, sp1, base_dir=d1, config={"dt_step": 0.25})
        with tempfile.TemporaryDirectory() as d_reuse:
            # First ensure an unsandboxed marker blocks reuse
            un_path = os.path.join(d1, "engine_build", "unsandboxed.json")
            if not os.path.exists(un_path):
                SIO.write_nofollow(d1, "engine_build/unsandboxed.json", b'{"events":[{"action":"run"}]}', atomic=True)
            blocked_un = try_reuse_admission(d1, d_reuse, key1)
            # Clear marker so reuse path can be verified (twice, checking idempotency over 0555 snapshot dir)
            SIO.remove_nofollow(d1, "engine_build/unsandboxed.json", missing_ok=True)
            reused = try_reuse_admission(d1, d_reuse, key1)
            reused2 = try_reuse_admission(d1, d_reuse, key1)
            miss_reuse = try_reuse_admission(d1, d_reuse, key_cal)
            ref_sha1 = (adm1.get("digests") or {}).get("reference_snapshot")
            ref_copied_ok = bool(
                ref_sha1 and BSNAP.read_verified_snapshot(
                    os.path.join(d_reuse, "engine_build", "reference", "snapshots"), ref_sha1
                )
            )
            # Removing the source reference snapshot manifest must make reuse fail closed
            SIO.remove_nofollow(d1, f"engine_build/reference/snapshots/{ref_sha1}.json", missing_ok=True)
            miss_ref = try_reuse_admission(d1, d_reuse, key1)
            ok(blocked_un is None and reused is not None and reused2 is not None and ref_copied_ok and miss_ref is None
               and reused["status"] == "admitted" and reused.get("reused_from") == os.path.abspath(d1)
               and not os.path.exists(os.path.join(d_reuse, "engine_build", "signoff.json"))
               and miss_reuse is None and key_cal != key1 and key_dep != key1
               and key_noref != key1 and key_cfg != key1,
               "BE32 reuse refuses unsandboxed/missing-ref runs, copies snapshots+ref idempotently (no signoff.json), and invalidates on changed inputs")
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

    # BE4: parameter wired to wrong output fails A6 and A7 cannot pass vacuously
    td, _, _, _, _, adm = _run_fixture("wrong_output", require_reference=False, with_ref=False)
    try:
        a6, a7 = adm["checks"]["A6"], adm["checks"]["A7"]
        ok(a6["status"] == "fail" and a7["status"] == "fail" and any("eff_a" in p for p in a6["problems"]),
           f"BE4 parameter wired to wrong output fails A6 and A7 cannot pass vacuously")
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

    # BE7: leaky run_batch_state fails A3
    td, _, _, _, _, adm = _run_fixture("leaky_batch", require_reference=False, with_ref=False)
    try:
        ok(adm["checks"]["A3"]["status"] == "fail", f"BE7 leaky batch path fails A3 ({adm['checks']['A3']['problems'][:1]})")
    finally:
        td.cleanup()

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

    # BE10: public lookup table fails S4 static check AND fails hidden A4 when static check bypassed
    td, _, b10, sp10, sha10, adm10 = _run_fixture("public_table", require_reference=False, with_ref=False)
    try:
        s4_caught = adm10["status"] == "not_admitted" and any("S4:" in p for p in adm10["static_problems"])
        cand10 = BCAND.CandidateEngine(BFIX.two_group_sir_files("public_table"), b10, unsandboxed_ok=dev_un)
        pub_a4 = check_a4(cand10, b10, sp10, _rng_from(PUBLIC_SEED_BYTES, "a4"), n_free=1, th=load_thresholds())
        hid_a4 = check_a4(cand10, b10, sp10, _rng_from(b"hidden-be10", "a4"), n_free=3, th=load_thresholds())
        ok(s4_caught and pub_a4["status"] == "pass" and hid_a4["status"] == "fail",
           f"BE10 public lookup table caught by S4, passes public A4, and fails hidden A4 (pub={pub_a4['status']}, hid={hid_a4['status']})")
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

    # BE17: reference off by 6% fails deterministic A11; stochastic fixture with 5% bias & huge noise is inconclusive
    td_det, _, _, _, _, adm_det = _run_fixture(None, ref_bias=0.06, require_reference=True, with_ref=True)
    b_st = BFIX.two_group_sir_build(stochastic=True)
    sp_st = BFIX.two_group_sir_spec(b_st)
    try:
        cand_st = BCAND.CandidateEngine(BFIX.two_group_sir_files("stochastic_bias", stochastic_sd=0.60),
                                        b_st, unsandboxed_ok=dev_un)
        ref_b = copy.deepcopy(b_st)
        ref_b["kind"] = "closed_form"
        ref_st = BCAND.CandidateEngine(BFIX.two_group_sir_reference_files(rel_bias=0.0), ref_b, unsandboxed_ok=dev_un)
        th_fast = dict(load_thresholds(), stochastic_seeds_init=4, stochastic_seeds_max=8)
        hor = _default_horizon(sp_st, b_st["request"])
        u_st = UnitSpec("u0", {"N": 10000.0, "s0": 0.9, "age_pop": [5000.0, 5000.0]})
        a11_st = check_a11(cand_st, ref_st, b_st, [({}, u_st, Package("p0", {"comp_a": 0}), hor)],
                           th=th_fast, required=True)
        ok(adm_det["checks"]["A11"]["status"] == "fail" and a11_st["status"] == "inconclusive",
           f"BE17 biased ref fails A11 ({adm_det['checks']['A11']['status']}); noisy biased stochastic is inconclusive ({a11_st['status']})")
    finally:
        td_det.cleanup()

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

    # Regression checks for Codex r1 fixes: _outcomes_close key parity, _default_horizon warm-up,
    # 30-bool package sampling without explosion, mutually exclusive option groups, A9 missing gating benchmark,
    # and missing required reference NOT triggering overfit_suspected
    empty_o = Outcomes({}, {}, {})
    non_empty_o = Outcomes({"cases": {"g0": 0.2}}, {"w0": {"prev": 0.1}}, {"d": 1.0})
    close_empty, _ = _outcomes_close(empty_o, non_empty_o)
    h_warm = _default_horizon({"horizon": {"start": 2010, "stop": 2035}, "objective": {"horizon": [2025, 2030]}},
                              {"outputs": {"windows": {"w0": ["prev"]}}})
    req_30 = {"components": {f"c{i:02d}": {"values": [0, 1], "absent": 0} for i in range(30)}, "unit_attributes": {}}
    _, pkgs_30 = _real_units_and_packages({}, req_30, max_packages=16, rng=np.random.default_rng(1))
    req_grp = {"components": {"a": {"values": [0, 1], "absent": 0}, "b": {"values": [0, 1], "absent": 0}}, "unit_attributes": {}}
    sp_grp = {
        "options": {
            "components": {"opt_a": {"group": "g1"}, "opt_b": {"group": "g1"}},
            "engine_map": {"opt_a": {"a": 1}, "opt_b": {"b": 1}},
            "packages": {"generate": True},
        }
    }
    _, pkgs_grp = _real_units_and_packages(sp_grp, req_grp, max_packages=16, rng=np.random.default_rng(1))
    both_active = any(pk.components.get("a") == 1 and pk.components.get("b") == 1 for pk in pkgs_grp)
    b_a9 = copy.deepcopy(BFIX.two_group_sir_build())
    b_a9["request"]["gating_benchmarks"] = ["B1"]
    a9_miss = check_a9(None, b_a9, BFIX.two_group_sir_spec(b_a9), dossier_benchmarks=[])
    td_nr, _, _, _, _, adm_nr = _run_fixture(None, require_reference=True, with_ref=False, run_public=True)
    try:
        nr_ok = (adm_nr["status"] == "not_admitted"
                 and adm_nr["checks"]["A11"]["status"] == "unavailable"
                 and adm_nr["overfit_suspected"] is False)
    finally:
        td_nr.cleanup()
    ok(not close_empty and h_warm.start == 2010.0 and h_warm.stop == 2035.0 and h_warm.window == (2025.0, 2030.0)
       and len(pkgs_30) == 16 and not both_active and a9_miss["status"] == "fail" and nr_ok,
       "BE_REG1 outcomes key parity, warm-up horizon, 30-bool/group packages, A9 missing gating, and non-overfit unavailable A11")

    # BE_CAL_ATTR1: _real_units_and_packages supplies interior midpoint for calibration.attribute (log & linear)
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
    u_log, _ = _real_units_and_packages(sp_cal_log, req_cal, rng=np.random.default_rng(1))
    sp_cal_lin = copy.deepcopy(sp_cal_log)
    sp_cal_lin["calibration"] = {"method": "posterior", "attribute": "rate", "scale": "linear", "range": [0.1, 0.3]}
    u_lin, _ = _real_units_and_packages(sp_cal_lin, req_cal, rng=np.random.default_rng(1))
    sp_unb = {"decision_units": sp_cal_log["decision_units"]}
    unb_refused = False
    try:
        _real_units_and_packages(sp_unb, req_cal, rng=np.random.default_rng(1))
    except ValueError:
        unb_refused = True
    ok(len(u_log) == 1 and abs(u_log[0].attributes["rate"] - math.exp(-4.0)) < 1e-12
       and len(u_lin) == 1 and abs(u_lin[0].attributes["rate"] - 0.2) < 1e-12 and unb_refused,
       f"BE_CAL_ATTR1 calibrated per-unit attribute gets prior midpoint in A1 real units while unbound non-calibrated fails")

    # BE_A6_COST_LOG: multi-decade positive continuous attrs sample in log space; non-decision-input attrs
    # affecting cost_drivers are evaluated under active component modes (wired passes, dead still fails).
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
            "parameters": [],
            "expected_effects": {"act": {"y": "-", "vol": "+"}, "scale": {"vol": "+"}},
            "stochastic": False,
        },
    }
    class _CdStub:
        kind = "closed_form"
        def __init__(self, wired: bool):
            self._wired = wired
        def run_many(self, cases):
            out = []
            for _prm, u, pk, _h, _s in cases:
                on = int(pk.components.get("act", 0))
                sc = float(u.attributes["scale"])
                vol = (0.5 * sc if self._wired else 10.0) if on else 0.0
                out.append(Outcomes({"y": {"g0": 1.0 - 0.5 * on}}, {}, {"vol": vol},
                                    meta={"age_shares": {"g0": 1.0}, "param_reads": {"build": [], "read_outcomes": []}}))
            return out
    th_cd = load_thresholds()
    a6_w, a7_w = check_a6_a7(_CdStub(True), b_cd, {}, np.random.default_rng(2), n_bases=3, min_moving=2, th=th_cd)
    a6_d, a7_d = check_a6_a7(_CdStub(False), b_cd, {}, np.random.default_rng(2), n_bases=3, min_moving=2, th=th_cd)
    ok(abs(log_mid - math.sqrt(0.0005 * 0.5)) < 1e-12 and abs(lin_mid - 0.55) < 1e-12
       and log_pts[1] < 0.01 and a6_w["status"] == "pass" and a7_w["status"] == "pass"
       and a6_d["status"] == "fail" and a7_d["status"] == "fail",
       f"BE_A6_COST_LOG log-space sampling for multi-decade attrs & active-mode A6 for cost_driver inputs "
       f"(log_mid={log_mid:.5f}, wired={a6_w['status']}/{a7_w['status']}, dead={a6_d['status']}/{a7_d['status']})")

    # BE_A7_CAT: unordered categorical string components compare active modes against `absent` (order-independent);
    # mixed-sign categorical violates A7; numeric ordered components still enforce step-by-step monotonicity.
    b_cat = {
        "id": "stub_cat", "kind": "closed_form",
        "request": {
            "components": {"mode": {"values": ["none", "mode_a", "mode_b"], "absent": "none"}},
            "unit_attributes": {"x": {"type": "continuous", "range": [0.1, 1.0]}},
            "outputs": {"per_capita": ["y_pos", "y_one"], "windows": {}, "cost_drivers": []},
            "age_groups": ["g0"],
            "parameters": [],
            "expected_effects": {"mode": {"y_pos": "+", "y_one": "+"}, "x": {"y_pos": "+"}},
            "stochastic": False,
        },
    }
    class _CatStub:
        kind = "closed_form"
        def __init__(self, *, bad_sign: bool = False, comp_key: str = "mode"):
            self._bad = bad_sign
            self._key = comp_key
        def run_many(self, cases):
            out = []
            for _prm, u, pk, _h, _s in cases:
                x = float(u.attributes["x"])
                mv = pk.components.get(self._key)
                if mv in ("none", 0):
                    yp, yo = 0.1 * x, 0.0
                elif mv in ("mode_a", 1):
                    yp, yo = 0.9 * x, 0.6 * x
                else:
                    yp = (0.02 * x) if self._bad else (0.5 * x)
                    yo = 0.0
                out.append(Outcomes({"y_pos": {"g0": yp}, "y_one": {"g0": yo}}, {}, {},
                                    meta={"age_shares": {"g0": 1.0}, "param_reads": {"build": [], "read_outcomes": []}}))
            return out
    a6_cg, a7_cg = check_a6_a7(_CatStub(bad_sign=False), b_cat, {}, np.random.default_rng(3), n_bases=3, min_moving=2, th=th_cd)
    a6_cb, a7_cb = check_a6_a7(_CatStub(bad_sign=True), b_cat, {}, np.random.default_rng(3), n_bases=3, min_moving=2, th=th_cd)
    b_num = copy.deepcopy(b_cat)
    b_num["request"]["components"] = {"level": {"values": [0, 1, 2], "absent": 0}}
    b_num["request"]["expected_effects"] = {"level": {"y_pos": "+"}, "x": {"y_pos": "+"}}
    b_num["request"]["outputs"]["per_capita"] = ["y_pos"]
    a6_nm, a7_nm = check_a6_a7(_CatStub(bad_sign=False, comp_key="level"), b_num, {}, np.random.default_rng(3), n_bases=3, min_moving=2, th=th_cd)
    ok(a6_cg["status"] == "pass" and a7_cg["status"] == "pass"
       and a7_cb["status"] == "fail" and a7_nm["status"] == "fail",
       f"BE_A7_CAT categorical string components compare active modes vs absent; mixed sign & non-monotone numeric fail A7 "
       f"(cat_ok={a6_cg['status']}/{a7_cg['status']}, cat_bad={a7_cb['status']}, num_nonmono={a7_nm['status']})")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
