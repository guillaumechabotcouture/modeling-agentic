#!/usr/bin/env python3
"""Claims-ledger provenance resolver (used by `scripts/build_claims_ledger.py`).

Resolvable sources (generic — no disease or domain knowledge):
  *.yaml / *.yml / *.json   dot path with optional list indices:
                            `scalar_outputs.total_cost.mean`, `folds[0].rmse`
  *.csv                     `<column>.<agg>()` with agg in sum, mean, median,
                            min, max, count, nunique; or `len()` / `count()`.
  *.py                      a module-level constant name assigned a literal.

Usage:
    python3 scripts/lib/provenance.py --self-test
"""

from __future__ import annotations

import ast
import csv
import json
import math
import os
import re
import statistics
import sys
from typing import Any, Optional

import yaml

# Phase 43 γ: one copy of the finite-number coercion, in safe_io.
try:
    from .safe_io import finite_number as _num
except ImportError:  # run as a script (--self-test): no parent package
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from lib.safe_io import finite_number as _num  # noqa: E402

DEFAULT_REL_TOL = 0.01      # ledger values may be rounded to ~3 s.f.
DEFAULT_ABS_TOL = 1e-9

_EXTERNAL_SOURCES = frozenset({"external", "literature", "published",
                               "citation", "citations"})
_PROSE_EXTENSIONS = (".md", ".txt", ".pdf", ".bib", ".docx", ".html")
_POINT_KEYS = ("mean", "value", "median", "point", "estimate")
_CSV_AGG_RE = re.compile(
    r"^\s*(?:(?P<col>[^.()]+?)\.)?(?P<agg>sum|mean|median|min|max|count|"
    r"nunique|len)\(\)\s*$")
# Optional row filter, either prefix `filter col='v'.` or suffix
# ` where col='v'` (one equality filter; quotes optional).
_CSV_FILTER_PREFIX_RE = re.compile(
    r"^\s*filter\s+(?P<fcol>[\w ]+?)\s*==?\s*(?P<fval>'[^']*'|\"[^\"]*\"|[^.\s]+)"
    r"\s*\.(?P<rest>.+)$")
_CSV_FILTER_SUFFIX_RE = re.compile(
    r"^(?P<rest>.+?)\s+where\s+(?P<fcol>[\w ]+?)\s*==?\s*"
    r"(?P<fval>'[^']*'|\"[^\"]*\"|\S+)\s*$")

_MISSING = object()


class _Unresolved(Exception):
    pass


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

_SELECTOR_RE = re.compile(r"\[\s*(?:(-?\d+)|([\w.\-]+)\s*=\s*(['\"]?)(.*?)\3)\s*\]")


def resolve_dotpath(doc: Any, path: str) -> Any:
    """Resolve `a.b[0].c` against nested dicts/lists. Raises _Unresolved.

    Keys may themselves contain dots (`cost_3.60.n` with key `cost_3.60`):
    at each mapping the longest key that prefixes the remaining path wins.
    A list may be indexed by position (`[0]`) or selected by a field
    (`results[id=foo].observed`; quotes around the value are optional).
    """
    cur, rest = doc, path.strip()
    if not rest:
        raise _Unresolved("empty field path")
    while rest:
        if rest[0] == ".":
            rest = rest[1:]
            continue
        if rest[0] == "[":
            m = _SELECTOR_RE.match(rest)
            if not m or not isinstance(cur, list):
                raise _Unresolved(f"bad selector at {rest!r} in {path!r}")
            if m.group(1) is not None:
                i = int(m.group(1))
                if not -len(cur) <= i < len(cur):
                    raise _Unresolved(f"index [{i}] out of range in {path!r}")
                cur = cur[i]
            else:
                k, v = m.group(2), m.group(4)
                hits = [e for e in cur if isinstance(e, dict) and str(e.get(k)) == v]
                if len(hits) != 1:
                    raise _Unresolved(f"selector [{k}={v}] matched {len(hits)} "
                                      f"elements in {path!r}")
                cur = hits[0]
            rest = rest[m.end():]
            continue
        if not isinstance(cur, dict):
            raise _Unresolved(f"{rest!r} applied to a non-mapping in {path!r}")
        keys = [str(k) for k in cur
                if rest == str(k) or rest.startswith(str(k) + ".")
                or rest.startswith(str(k) + "[")]
        if not keys:
            raise _Unresolved(f"key {rest.split('.')[0].split('[')[0]!r} "
                              f"not found in {path!r}")
        k = max(keys, key=len)
        cur = cur[next(x for x in cur if str(x) == k)]
        rest = rest[len(k):]
    return cur


def _load_structured(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        if path.endswith(".json"):
            return json.load(f)
        return yaml.safe_load(f)


def _csv_aggregate(path: str, field: str) -> float:
    flt = None
    fm = _CSV_FILTER_PREFIX_RE.match(field) or _CSV_FILTER_SUFFIX_RE.match(field)
    if fm:
        fval = fm.group("fval").strip()
        if len(fval) >= 2 and fval[0] == fval[-1] and fval[0] in "'\"":
            fval = fval[1:-1]
        flt = (fm.group("fcol").strip(), fval)
        field = fm.group("rest")
    m = _CSV_AGG_RE.match(field)
    if not m:
        raise _Unresolved(
            f"CSV source_field {field!r} must be `<column>.<agg>()` "
            f"(agg: sum|mean|median|min|max|count|nunique) or `len()`, "
            f"optionally with one row filter `... where <col>='<value>'`")
    col, agg = m.group("col"), m.group("agg")
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if flt is not None:
        if rows and flt[0] not in rows[0]:
            raise _Unresolved(f"filter column {flt[0]!r} not in "
                              f"{os.path.basename(path)}")
        rows = [r for r in rows if str(r.get(flt[0], "")).strip() == flt[1]]
    if col is None or agg == "len":
        if agg not in ("len", "count"):
            raise _Unresolved(f"{agg}() requires a column")
        return float(len(rows))
    col = col.strip()
    if rows and col not in rows[0]:
        raise _Unresolved(f"column {col!r} not in {os.path.basename(path)} "
                          f"(columns: {list(rows[0].keys())[:10]})")
    raw = [r.get(col) for r in rows]
    if agg == "count":
        return float(sum(1 for v in raw if v not in (None, "")))
    if agg == "nunique":
        return float(len({v for v in raw if v not in (None, "")}))
    vals = []
    for v in raw:
        if v in (None, ""):
            continue
        try:
            vals.append(float(v))
        except ValueError:
            raise _Unresolved(f"column {col!r} has non-numeric value {v!r}")
    if not vals:
        raise _Unresolved(f"column {col!r} has no numeric values")
    if agg == "sum":
        return float(math.fsum(vals))
    if agg == "mean":
        return float(statistics.fmean(vals))
    if agg == "median":
        return float(statistics.median(vals))
    if agg == "min":
        return float(min(vals))
    return float(max(vals))


def _py_constant(path: str, name: str) -> Any:
    name = name.strip()
    if not name.isidentifier():
        raise _Unresolved(f"Python source_field {name!r} must be a "
                          f"module-level constant name")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    found: Any = _MISSING
    for node in tree.body:
        targets: list = []
        value = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        for t in targets:
            if isinstance(t, ast.Name) and t.id == name:
                try:
                    found = ast.literal_eval(value)
                except (ValueError, SyntaxError):
                    raise _Unresolved(f"{name} is not assigned a literal")
    if found is _MISSING:
        raise _Unresolved(f"constant {name!r} not found at module level")
    return found


def _resolve_artifact_path(run_dir: str, source_artifact: str) -> Optional[str]:
    """The cited path; else, only where the two can name the same artifact
    (a bare filename, or run root <-> models/), its sibling. A missing
    `data/results.yaml` must not resolve to an unrelated `models/results.yaml`."""
    cand = [os.path.join(run_dir, source_artifact)]
    base = os.path.basename(source_artifact)
    parent = os.path.dirname(os.path.normpath(source_artifact))
    if parent in ("", ".", "models"):
        cand += [os.path.join(run_dir, base), os.path.join(run_dir, "models", base)]
    for c in cand:
        if os.path.isfile(c):
            return c
    return None


def is_external(source_artifact: Any) -> bool:
    s = str(source_artifact or "").strip()
    return s.lower() in _EXTERNAL_SOURCES or s.lower().endswith(_PROSE_EXTENSIONS)


def _split_point_ci(resolved: Any, doc: Any, field: str
                    ) -> tuple[Any, Optional[tuple[Any, Any]]]:
    """Return (point_value, (lo, hi) or None) from a resolved node."""
    if isinstance(resolved, dict):
        point = _MISSING
        for k in _POINT_KEYS:
            if k in resolved:
                point = resolved[k]
                break
        return point, _ci_from_mapping(resolved)
    # Leaf: look for CI siblings when the path ends in a point key.
    for k in _POINT_KEYS:
        if field.endswith("." + k):
            try:
                parent = resolve_dotpath(doc, field[:-len(k) - 1])
            except _Unresolved:
                break
            if isinstance(parent, dict):
                return resolved, _ci_from_mapping(parent)
            break
    return resolved, None


def _ci_from_mapping(m: dict) -> Optional[tuple[Any, Any]]:
    for lo_k, hi_k in (("ci_low", "ci_high"), ("ci_lower", "ci_upper"),
                       ("lower", "upper"), ("lo", "hi")):
        if lo_k in m and hi_k in m:
            return m[lo_k], m[hi_k]
    for k in ("ci_95", "ci95", "ci"):
        v = m.get(k)
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return v[0], v[1]
    return None


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #

def _close(a: float, b: float, rel_tol: float, abs_tol: float) -> bool:
    return math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)


def values_match(claimed: Any, resolved: Any, claim_kind: str = "",
                 rel_tol: float = DEFAULT_REL_TOL,
                 abs_tol: float = DEFAULT_ABS_TOL) -> bool:
    a, b = _num(claimed), _num(resolved)
    if a is not None and b is not None:
        if _close(a, b, rel_tol, abs_tol):
            return True
        if claim_kind == "percentage":
            # 25 (percent) vs 0.25 (fraction), either direction.
            return _close(a, b * 100, rel_tol, abs_tol) or \
                _close(a * 100, b, rel_tol, abs_tol)
        return False
    if a is not None or b is not None:
        return False  # one numeric, one not

    def norm(s: Any) -> str:
        return re.sub(r"[\s_\-]+", " ", str(s)).strip().upper()
    return norm(claimed) == norm(resolved)


# --------------------------------------------------------------------------- #
# Main entry
# --------------------------------------------------------------------------- #


class ClaimUnresolved(Exception):
    """A claim's source cannot be resolved. `path` is the artifact when it
    exists (so callers can still search it), else None."""

    def __init__(self, msg: str, path: Optional[str] = None):
        super().__init__(msg)
        self.path = path


def resolve_claim(c: dict, run_dir: str, cache: Optional[dict] = None
                  ) -> tuple[Any, Optional[tuple[Any, Any]]]:
    """Resolve one non-external claim to (point, (ci_low, ci_high) | None).

    Shared by check_ledger_provenance (verify a hand-written ledger) and
    scripts/build_claims_ledger.py (derive the ledger from a spec, so the
    numbers are never typed by an agent). Raises ClaimUnresolved.
    """
    cache = {} if cache is None else cache
    src, field = c.get("source_artifact"), c.get("source_field")
    if not src or field in (None, ""):
        raise ClaimUnresolved(
            "has no source_artifact/source_field; every model-derived claim "
            "must cite the artifact field that holds it (or `source_artifact: "
            "external` for literature values).")
    path = _resolve_artifact_path(run_dir, str(src))
    if path is None:
        raise ClaimUnresolved(f"source_artifact `{src}` does not exist in the run.")
    field = str(field)
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext in (".yaml", ".yml", ".json"):
            if path not in cache:
                cache[path] = _load_structured(path)
            doc = cache[path]
            resolved = resolve_dotpath(doc, field)
            point, ci = _split_point_ci(resolved, doc, field)
            ci_field = c.get("ci_source_field")
            if ci_field:
                node = resolve_dotpath(doc, str(ci_field))
                if isinstance(node, dict):
                    ci = _ci_from_mapping(node)
                elif isinstance(node, (list, tuple)) and len(node) == 2:
                    ci = (node[0], node[1])
                else:
                    raise _Unresolved(f"ci_source_field {ci_field!r} is not a CI")
        elif ext == ".csv":
            point, ci = _csv_aggregate(path, field), None
        elif ext == ".py":
            point, ci = _py_constant(path, field), None
        else:
            raise _Unresolved(f"unsupported source type {ext!r}")
    except _Unresolved as e:
        raise ClaimUnresolved(
            f"source_field {field!r} in `{src}` cannot be resolved: {e}. "
            f"Use a machine-resolvable field path.", path) from None
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError,
            SyntaxError, csv.Error) as e:
        raise ClaimUnresolved(f"`{src}` could not be read: {type(e).__name__}: {e}") from None
    if point is _MISSING:
        raise ClaimUnresolved(
            f"`{src}::{field}` is a mapping with no {'/'.join(_POINT_KEYS)} key "
            f"to compare against.", path)
    return point, ci


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

def _self_test() -> int:
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "uncertainty_report.yaml"), "w") as f:
            yaml.safe_dump({"n_draws": 200, "scalar_outputs": {
                "total_outcome": {"mean": 1769757.25, "ci_low": 1769757.25,
                                  "ci_high": 1769757.25},
                "total_cost": {"mean": 3.2e8, "ci_low": 3.0e8,
                               "ci_high": 3.4e8}}}, f)
        with open(os.path.join(d, "models", "allocation.csv"), "w") as f:
            f.write("unit,cost,outcome\nA,100,5\nB,200,7\nC,,1\n")
        with open(os.path.join(d, "models", "params.py"), "w") as f:
            f.write("UNIT_COST = 2.5\nRATE: float = 0.3\nDERIVED = UNIT_COST * 2\n")
        with open(os.path.join(d, "models", "sens.yaml"), "w") as f:
            yaml.safe_dump({"verdict": "SENSITIVE",
                            "folds": [{"rmse": 0.1}, {"rmse": 0.2}]}, f)

        # P2/P3: dotpath leaf + mapping node with sibling CI
        pt, ci = resolve_claim({"source_artifact": "uncertainty_report.yaml",
                                "source_field": "scalar_outputs.total_cost.mean"}, d)
        ok(values_match(3.2e8, pt, "currency") and ci == (3.0e8, 3.4e8), f"P2 dotpath + CI: {(pt, ci)}")
        pt_m, ci_m = resolve_claim({"source_artifact": "uncertainty_report.yaml",
                                    "source_field": "scalar_outputs.total_cost"}, d)
        ok(values_match(321000000, pt_m, "currency") and ci_m == (3.0e8, 3.4e8), f"P3 mapping node: {(pt_m, ci_m)}")

        # P4d: no basename fallback across unrelated directories
        os.makedirs(os.path.join(d, "data"), exist_ok=True)
        try:
            resolve_claim({"source_artifact": "data/sens.yaml", "source_field": "verdict"}, d)
            ok(False, "P4d data/ path must not alias to models/")
        except ClaimUnresolved:
            ok(True, "P4d")

        # P4c: dotted keys + [key=value] selectors
        with open(os.path.join(d, "models", "scen.yaml"), "w") as f:
            yaml.safe_dump({"cost_3.60": {"n_changed": 248},
                            "results": [{"id": "a", "observed": 47.1}, {"id": "b", "observed": 9}]}, f)
        ok(resolve_claim({"source_artifact": "models/scen.yaml", "source_field": "cost_3.60.n_changed"}, d)[0] == 248, "P4c dotted key")
        ok(resolve_claim({"source_artifact": "models/scen.yaml", "source_field": "results[id=a].observed"}, d)[0] == 47.1, "P4c selector")
        ok(resolve_claim({"source_artifact": "models/scen.yaml", "source_field": "results[id='b'].observed"}, d)[0] == 9, "P4c quoted selector")

        # P5: missing artifact raises ClaimUnresolved
        try:
            resolve_claim({"source_artifact": "models/nope.yaml", "source_field": "x"}, d)
            ok(False, "P5 missing artifact")
        except ClaimUnresolved:
            ok(True, "P5")

        # P6/P6b: CSV aggregations and filters
        ok(resolve_claim({"source_artifact": "models/allocation.csv", "source_field": "cost.sum()"}, d)[0] == 300.0, "P6 sum")
        ok(resolve_claim({"source_artifact": "models/allocation.csv", "source_field": "len()"}, d)[0] == 3.0, "P6 len")
        ok(resolve_claim({"source_artifact": "models/allocation.csv", "source_field": "filter unit='B'.cost.sum()"}, d)[0] == 200.0, "P6b prefix")
        ok(resolve_claim({"source_artifact": "models/allocation.csv", "source_field": "outcome.sum() where unit=A"}, d)[0] == 5.0, "P6b suffix")
        try:
            resolve_claim({"source_artifact": "models/allocation.csv", "source_field": "outcome.sum() where nocol=A"}, d)
            ok(False, "P6b bad filter column")
        except ClaimUnresolved:
            ok(True, "P6b bad col")

        # P8: Python constants; non-literal expression rejected
        ok(resolve_claim({"source_artifact": "models/params.py", "source_field": "UNIT_COST"}, d)[0] == 2.5, "P8 const")
        ok(resolve_claim({"source_artifact": "models/params.py", "source_field": "RATE"}, d)[0] == 0.3, "P8 annconst")
        try:
            resolve_claim({"source_artifact": "models/params.py", "source_field": "DERIVED"}, d)
            ok(False, "P8 non-literal expression")
        except ClaimUnresolved:
            ok(True, "P8 non-literal")

        # P9/P10/P11/P12/P13
        pt9, _ = resolve_claim({"source_artifact": "models/sens.yaml", "source_field": "verdict"}, d)
        ok(values_match("sensitive", pt9, "label") and not values_match("ROBUST", pt9, "label"), "P9 label match")
        ok(resolve_claim({"source_artifact": "models/sens.yaml", "source_field": "folds[1].rmse"}, d)[0] == 0.2, "P10 list index")
        ok(is_external("external") and is_external("citations.md") and not is_external("models/sens.yaml"), "P11 is_external")
        ok(values_match(25, 0.25, "percentage") and values_match(0.25, 25, "percentage"), "P12 percent vs fraction")
        pt13, ci13 = resolve_claim({"source_artifact": "uncertainty_report.yaml",
                                    "source_field": "n_draws",
                                    "ci_source_field": "scalar_outputs.total_cost"}, d)
        ok(pt13 == 200 and ci13 == (3.0e8, 3.4e8), f"P13 ci_source_field: {(pt13, ci13)}")

    if failures:
        print("FAIL:", file=sys.stderr)
        for fl in failures:
            print(f"  - {fl}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        return _self_test()
    print("ERROR: provenance.py is a library module; pass --self-test to verify.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
