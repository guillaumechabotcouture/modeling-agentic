#!/usr/bin/env python3
"""
Phase 21 β — claims-ledger provenance check.

Phase 18 α made the writer bind every number to `models/claims_ledger.yaml`,
but nothing verified that the ledger itself matched the artifacts it cites.
In 2026-05-03_235631 the ledger claimed a DALY mean of 1,806,250 with a 95%
CI of 749,219–2,900,144 sourced from `uncertainty_report.yaml`, while that
file recorded mean = ci_low = ci_high = 1,769,757 (a zero-width interval).
The report rendered the ledger faithfully — so every downstream gate saw a
perfectly "bound" headline number that its own source contradicted.

This module resolves each claim's `source_artifact` + `source_field` and
compares the resolved value (and CI) to the ledger:

  claim_source_unresolved  HIGH  artifact missing, or field cannot be resolved
  claim_source_mismatch    HIGH  resolved value differs from ledger value
  claim_value_not_in_source HIGH field unresolvable and the value appears
                                nowhere in the cited artifact
  claim_ci_unsourced       HIGH  ledger carries a CI with no resolvable CI source
  claim_ci_mismatch        HIGH  ledger CI differs from the source CI

None are scope-declarable: the fix (point the claim at the artifact that
actually holds the number, or correct the number) is always available.

Resolvable sources (generic — no disease or domain knowledge):
  *.yaml / *.yml / *.json   dot path with optional list indices:
                            `scalar_outputs.total_cost.mean`, `folds[0].rmse`
                            A path resolving to a mapping uses its
                            mean/value/median/point key for the value and
                            ci_low/ci_high (or ci_95: [lo, hi]) for the CI.
                            If the path ends in mean/median/value/point, the
                            sibling ci_low/ci_high keys supply the CI.
                            An explicit `ci_source_field` (same grammar)
                            overrides both.
  *.csv                     `<column>.<agg>()` with agg in sum, mean, median,
                            min, max, count, nunique; or `len()` / `count()`
                            for the row count. One optional equality row
                            filter: `cost.sum() where group='A'` or
                            `filter group='A'.cost.sum()`.
  *.py                      a module-level constant name assigned a literal.

Non-resolvable sources are skipped and counted, not failed: `external`,
`literature`, and prose documents (*.md, *.txt, *.pdf, *.bib, *.docx).
Literature values are checked by the effect-size registry and red-team,
not here. A model output must cite a machine-readable artifact; a derived
number (ratio, difference) must be written to an artifact first.

Usage:
    python3 scripts/lib/provenance.py <run_dir>     # prints findings
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


def _numeric_leaves(x: Any, out: list) -> list:
    if isinstance(x, dict):
        for v in x.values():
            _numeric_leaves(v, out)
    elif isinstance(x, (list, tuple)):
        for v in x:
            _numeric_leaves(v, out)
    elif _num(x) is not None:
        out.append(_num(x))
    return out


def value_in_artifact(path: str, value: Any, claim_kind: str = "",
                      rel_tol: float = DEFAULT_REL_TOL) -> Optional[bool]:
    """Does the claimed number appear anywhere in the artifact? None when
    the value isn't numeric or the file can't be scanned."""
    if _num(value) is None:
        return None
    ext = os.path.splitext(path)[1].lower()
    try:
        if os.path.getsize(path) > 50 * 1024 * 1024:
            return None
        if ext in (".yaml", ".yml", ".json"):
            nums = _numeric_leaves(_load_structured(path), [])
        elif ext == ".csv":
            with open(path, encoding="utf-8", newline="") as f:
                nums = [n for row in csv.reader(f) for n in map(_num, row) if n is not None]
        else:
            return None
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError, csv.Error):
        return None
    return any(values_match(value, n, claim_kind, rel_tol) for n in nums)


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

def _f(kind: str, claim: str) -> dict:
    return {"kind": kind, "severity": "HIGH", "stage": "ANALYZE",
            "claim": claim, "scope_declarable": False}


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


def check_ledger_provenance(ledger: Any, run_dir: str,
                            rel_tol: float = DEFAULT_REL_TOL,
                            abs_tol: float = DEFAULT_ABS_TOL) -> dict:
    """Verify every ledger claim against its cited artifact.

    `ledger` is the parsed YAML document ({"claims": [...]}) or a list of
    claim dicts. Returns {"findings": [...], "n_checked": int,
    "n_external": int, "n_total": int}.
    """
    claims = ledger.get("claims") if isinstance(ledger, dict) else ledger
    findings: list[dict] = []
    n_checked = n_external = 0
    if not isinstance(claims, list):
        return {"findings": [_f("claim_source_unresolved",
                                "claims_ledger.yaml has no `claims:` list")],
                "n_checked": 0, "n_external": 0, "n_total": 0}
    cache: dict[str, Any] = {}

    for c in claims:
        if not isinstance(c, dict):
            continue
        cid = c.get("id", "<no id>")
        src = c.get("source_artifact")
        field = c.get("source_field")
        kind = str(c.get("claim_kind") or "")
        if is_external(src):
            n_external += 1
            continue
        try:
            point, ci = resolve_claim(c, run_dir, cache)
        except ClaimUnresolved as e:
            # The path can't be followed, but the number can still be looked
            # for: a value absent from its whole cited artifact is a
            # mismatch, not just an unverifiable claim.
            found = value_in_artifact(e.path, c.get("value"), kind, rel_tol) if e.path else None
            if found is False:
                findings.append(_f(
                    "claim_value_not_in_source",
                    f"[{cid}] ledger value {c.get('value')!r} appears nowhere in "
                    f"`{src}` ({e}). Correct the value or cite the artifact that "
                    f"holds it."))
            else:
                findings.append(_f("claim_source_unresolved", f"[{cid}] {e}"))
            continue

        n_checked += 1
        if not values_match(c.get("value"), point, kind, rel_tol, abs_tol):
            findings.append(_f(
                "claim_source_mismatch",
                f"[{cid}] ledger value {c.get('value')!r} does not match "
                f"`{src}::{field}` = {point!r} (rel_tol {rel_tol:g})."))

        lo, hi = c.get("ci_low"), c.get("ci_high")
        if lo is None and hi is None:
            continue
        if ci is None:
            findings.append(_f(
                "claim_ci_unsourced",
                f"[{cid}] ledger carries a CI [{lo}, {hi}] but `{src}::"
                f"{field}` has no ci_low/ci_high to verify it against. Cite "
                f"the uncertainty artifact (ci_source_field) or drop the CI."))
            continue
        for label, claimed, src_v in (("ci_low", lo, ci[0]),
                                      ("ci_high", hi, ci[1])):
            if not values_match(claimed, src_v, kind, rel_tol, abs_tol):
                findings.append(_f(
                    "claim_ci_mismatch",
                    f"[{cid}] ledger {label} {claimed!r} does not match "
                    f"source {label} {src_v!r} (`{src}::{field}`)."))

    return {"findings": findings, "n_checked": n_checked,
            "n_external": n_external, "n_total": len(claims)}


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

def _self_test() -> int:
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    def kinds(res: dict) -> list[str]:
        return [f["kind"] for f in res["findings"]]

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

        def run(*claims):
            return check_ledger_provenance({"claims": list(claims)}, d)

        # P1: the 235631 class — ledger mean/CI contradict the UQ artifact.
        r = run({"id": "c1", "claim_kind": "scalar", "value": 1806250,
                 "ci_low": 749219, "ci_high": 2900144,
                 "source_artifact": "uncertainty_report.yaml",
                 "source_field": "scalar_outputs.total_outcome.mean"})
        ok("claim_source_mismatch" in kinds(r), "P1 value mismatch fires")
        ok(kinds(r).count("claim_ci_mismatch") == 2, "P1b both CI bounds fire")

        # P2: matching value + CI via sibling keys → clean.
        r = run({"id": "c2", "claim_kind": "currency", "value": 3.2e8,
                 "ci_low": 3.0e8, "ci_high": 3.4e8,
                 "source_artifact": "uncertainty_report.yaml",
                 "source_field": "scalar_outputs.total_cost.mean"})
        ok(kinds(r) == [], f"P2 matching claim is clean ({kinds(r)})")

        # P3: path to mapping uses its mean + CI.
        r = run({"id": "c3", "claim_kind": "currency", "value": 321000000,
                 "ci_low": 3.0e8, "ci_high": 3.4e8,
                 "source_artifact": "uncertainty_report.yaml",
                 "source_field": "scalar_outputs.total_cost"})
        ok(kinds(r) == [], f"P3 mapping node + rounding tolerance ({kinds(r)})")

        # P4: free-text source_field, value present in the file → unresolved.
        r = run({"id": "c4", "claim_kind": "scalar", "value": 0.2,
                 "source_artifact": "models/sens.yaml",
                 "source_field": "zone-level Pearson correlation"})
        ok(kinds(r) == ["claim_source_unresolved"], f"P4 prose field unresolved ({kinds(r)})")
        # P4b: free-text field, value nowhere in the file → not-in-source.
        r = run({"id": "c4b", "claim_kind": "scalar", "value": 0.37,
                 "source_artifact": "models/sens.yaml",
                 "source_field": "zone-level Pearson correlation"})
        ok(kinds(r) == ["claim_value_not_in_source"], f"P4b value absent from source ({kinds(r)})")
        # P4d (Codex review): no basename fallback across unrelated directories.
        os.makedirs(os.path.join(d, "data"), exist_ok=True)
        r = run({"id": "c4f", "claim_kind": "label", "value": "SENSITIVE",
                 "source_artifact": "data/sens.yaml", "source_field": "verdict"})
        ok(kinds(r) == ["claim_source_unresolved"], f"P4d data/ path not aliased to models/ ({kinds(r)})")
        # P4c: keys with dots and [key=value] selectors resolve (E5 ledger plants).
        with open(os.path.join(d, "models", "scen.yaml"), "w") as f:
            yaml.safe_dump({"cost_3.60": {"n_changed": 248},
                            "results": [{"id": "a", "observed": 47.1}, {"id": "b", "observed": 9}]}, f)
        r = run({"id": "c4c", "value": 340, "source_artifact": "models/scen.yaml",
                 "source_field": "cost_3.60.n_changed"},
                {"id": "c4d", "value": 47.1, "source_artifact": "models/scen.yaml",
                 "source_field": "results[id=a].observed"},
                {"id": "c4e", "value": 9, "source_artifact": "models/scen.yaml",
                 "source_field": "results[id='b'].observed"})
        ok(kinds(r) == ["claim_source_mismatch"] and "c4c" in r["findings"][0]["claim"],
           f"P4c dotted key + selectors resolve ({kinds(r)})")

        # P5: missing artifact → unresolved.
        r = run({"id": "c5", "claim_kind": "scalar", "value": 1,
                 "source_artifact": "models/nope.yaml", "source_field": "x"})
        ok(kinds(r) == ["claim_source_unresolved"], "P5 missing artifact")

        # P6: CSV aggregations (empty cell skipped).
        r = run({"id": "c6", "claim_kind": "currency", "value": 300,
                 "source_artifact": "models/allocation.csv",
                 "source_field": "cost.sum()"},
                {"id": "c6b", "claim_kind": "count", "value": 3,
                 "source_artifact": "models/allocation.csv",
                 "source_field": "len()"},
                {"id": "c6c", "claim_kind": "scalar", "value": 99,
                 "source_artifact": "models/allocation.csv",
                 "source_field": "outcome.sum()"})
        ok(kinds(r) == ["claim_source_mismatch"],
           f"P6 CSV sum/len resolve; wrong sum fires ({kinds(r)})")
        r = run({"id": "c6d", "claim_kind": "currency", "value": 200,
                 "source_artifact": "models/allocation.csv",
                 "source_field": "filter unit='B'.cost.sum()"},
                {"id": "c6e", "claim_kind": "scalar", "value": 5,
                 "source_artifact": "models/allocation.csv",
                 "source_field": "outcome.sum() where unit=A"},
                {"id": "c6f", "claim_kind": "scalar", "value": 1,
                 "source_artifact": "models/allocation.csv",
                 "source_field": "outcome.sum() where nocol=A"})
        ok(kinds(r) == ["claim_source_unresolved"],
           f"P6b CSV row filters (prefix/suffix; bad column) ({kinds(r)})")

        # P7: CSV with CI but no CI source → unsourced.
        r = run({"id": "c7", "claim_kind": "scalar", "value": 13,
                 "ci_low": 5, "ci_high": 20,
                 "source_artifact": "models/allocation.csv",
                 "source_field": "outcome.sum()"})
        ok(kinds(r) == ["claim_ci_unsourced"], f"P7 CI unsourced ({kinds(r)})")

        # P8: Python constant; non-literal → unresolved.
        r = run({"id": "c8", "claim_kind": "currency", "value": 2.5,
                 "source_artifact": "models/params.py",
                 "source_field": "UNIT_COST"},
                {"id": "c8b", "claim_kind": "scalar", "value": 0.3,
                 "source_artifact": "models/params.py",
                 "source_field": "RATE"},
                {"id": "c8c", "claim_kind": "scalar", "value": 5,
                 "source_artifact": "models/params.py",
                 "source_field": "DERIVED"})
        ok(kinds(r) == ["claim_source_unresolved"],
           f"P8 py constants resolve; expression rejected ({kinds(r)})")

        # P9: labels compare case/underscore-insensitively.
        r = run({"id": "c9", "claim_kind": "label", "value": "sensitive",
                 "source_artifact": "models/sens.yaml",
                 "source_field": "verdict"},
                {"id": "c9b", "claim_kind": "label", "value": "ROBUST",
                 "source_artifact": "models/sens.yaml",
                 "source_field": "verdict"})
        ok(kinds(r) == ["claim_source_mismatch"],
           f"P9 label match + ROBUST-vs-SENSITIVE mismatch ({kinds(r)})")

        # P10: list index path.
        r = run({"id": "c10", "claim_kind": "scalar", "value": 0.2,
                 "source_artifact": "models/sens.yaml",
                 "source_field": "folds[1].rmse"})
        ok(kinds(r) == [], f"P10 list index ({kinds(r)})")

        # P11: external / prose sources skipped and counted.
        r = run({"id": "c11", "claim_kind": "scalar", "value": 0.5,
                 "source_artifact": "external", "source_field": "Smith 2020"},
                {"id": "c11b", "claim_kind": "scalar", "value": 0.5,
                 "source_artifact": "citations.md", "source_field": "x"})
        ok(kinds(r) == [] and r["n_external"] == 2, "P11 external skipped")

        # P12: percentage fraction/percent equivalence.
        with open(os.path.join(d, "models", "share.yaml"), "w") as f:
            yaml.safe_dump({"share": 0.25}, f)
        r = run({"id": "c12", "claim_kind": "percentage", "value": 25,
                 "source_artifact": "models/share.yaml",
                 "source_field": "share"})
        ok(kinds(r) == [], "P12 percent vs fraction")

        # P13: explicit ci_source_field.
        r = run({"id": "c13", "claim_kind": "scalar", "value": 13,
                 "ci_low": 3.0e8, "ci_high": 3.4e8,
                 "source_artifact": "uncertainty_report.yaml",
                 "source_field": "n_draws",
                 "ci_source_field": "scalar_outputs.total_cost"})
        ok("claim_ci_unsourced" not in kinds(r) and
           "claim_source_mismatch" in kinds(r),
           f"P13 ci_source_field honored ({kinds(r)})")

        # P14: no source at all → unresolved; non-list ledger → unresolved.
        r = run({"id": "c14", "claim_kind": "scalar", "value": 1})
        ok(kinds(r) == ["claim_source_unresolved"], "P14 no source")
        r = check_ledger_provenance({"claims": "oops"}, d)
        ok(kinds(r) == ["claim_source_unresolved"], "P14b non-list ledger")

        ok(all(f["scope_declarable"] is False and f["severity"] == "HIGH"
               for f in run({"id": "x", "value": 1}).get("findings")),
           "P15 findings are HIGH + non-declarable")

    if failures:
        print("FAIL:", file=sys.stderr)
        for fl in failures:
            print(f"  - {fl}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--self-test" in args:
        return _self_test()
    if len(args) != 1:
        print(__doc__)
        return 2
    run_dir = args[0]
    path = os.path.join(run_dir, "models", "claims_ledger.yaml")
    with open(path, encoding="utf-8") as f:
        ledger = yaml.safe_load(f)
    res = check_ledger_provenance(ledger, run_dir)
    for fnd in res["findings"]:
        print(f"[{fnd['severity']}] {fnd['kind']}: {fnd['claim']}")
    print(f"checked={res['n_checked']} external={res['n_external']} "
          f"total={res['n_total']} findings={len(res['findings'])}")
    return 1 if res["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
