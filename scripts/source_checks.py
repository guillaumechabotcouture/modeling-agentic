#!/usr/bin/env python3
"""
E5 follow-up — source checks for unit and data-vintage errors.

E5 (docs/experiments/E5_critic_effectiveness.md) planted known defects in
frozen CRITIQUE inputs. Three classes were caught by no critic and no gate
more than half the time: ledger mismatches (fixed in lib/provenance.py and
the claims-ledger gate), unit errors, and data-vintage errors. Both of the
latter need a value checked against its source, not prose read carefully,
so they are checked here mechanically. Everything is disease-agnostic: the
checks compare the run's data with itself and with what its own documents
say, using no domain vocabulary.

Unit checks (data CSVs anywhere in the run, except figures/):
  unit_bound_order_violated  HIGH  `<x>_lo` / `<x>_hi` (or _low/_high,
                                   _lower/_upper, _lb/_ub) don't bracket `<x>`
                                   on >= 20% of rows
  unit_scale_mismatch        HIGH  the same column name in two CSVs differs
                                   in median by a power of ten (>= 100x)
  unit_doc_scale_mismatch    HIGH  a document quotes `<column> = v` and the
                                   column holds v only after x/÷ 100 or 1000
  unit_out_of_range          MEDIUM a proportion-named column (coverage,
                                   share, fraction, percent, prevalence,
                                   probability, …) exceeds 100, or a
                                   `per_<N>` column exceeds N. MEDIUM because
                                   names mislead (`u5_dhs_share` holding a
                                   count derived from a share).

Vintage check (top-level and data/ markdown, plus data file names):
  data_vintage_conflict      MEDIUM a source named with a year once, while
                                   the same source is named with another
                                   year (>= 5 years apart) at least twice,
                                   and no data file carries the odd year

MEDIUM for vintage because sources legitimately appear with several years
(survey rounds); the finding quotes the conflicting mentions so a reader
can decide quickly.

Usage:
    python3 scripts/source_checks.py <run_dir>
    python3 scripts/source_checks.py --self-test
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import statistics
import sys
from collections import defaultdict

MAX_CSV_BYTES = 50 * 1024 * 1024
MAX_CSVS = 300
MAX_ROWS = 20000
_SKIP_DIRS = {"figures", "__pycache__", ".git", "_replay", "node_modules"}

_NUM_RE = r"-?\d+(?:,\d{3})*(?:\.\d+)?"
_BOUND_SUFFIXES = (("_lo", "_hi"), ("_low", "_high"), ("_lower", "_upper"),
                   ("_lb", "_ub"), ("_min", "_max"))
_PROPORTION_TOKENS = {"coverage", "proportion", "prop", "fraction", "frac",
                      "share", "pct", "percent", "percentage", "prevalence",
                      "probability"}
_PER_N_RE = re.compile(r"per_?(\d+(?:\.\d+)?)(k|m|million|thousand)?\b", re.I)
_PER_N_MULT = {None: 1, "k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}

# Data sources are cited by acronym plus round year ("DHS 2018"). Author-year
# citations belong to the citation checks; currency/fiscal prefixes date a
# price or budget, not a dataset.
_YEAR_STOP = {"USD", "EUR", "GBP", "FY", "TBD", "NA", "ID", "UTC", "AD", "BC"}
# A year that opens a range ("2000-2014", "2010/15") or is part of a list of
# rounds is not a single vintage.
_YEAR_MENTION_RE = re.compile(
    r"\b([A-Z]{2,10})[\s\-–]+((?:19|20)\d\d)\b(?!\s*[-–/]\s*\d)")
_FILE_YEAR_RE = re.compile(r"([a-z]{2,20})[_\-]?((?:19|20)\d\d)")


def _f(kind: str, severity: str, claim: str, file: str = "") -> dict:
    out = {"kind": kind, "severity": severity, "stage": "DATA", "claim": claim}
    if file:
        out["file"] = file
    return out


def _num(s) -> float | None:
    try:
        x = float(str(s).replace(",", "").strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def _walk(run_dir: str, ext: str) -> list[str]:
    out = []
    for root, dirs, files in os.walk(run_dir):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for fn in sorted(files):
            if fn.lower().endswith(ext):
                out.append(os.path.join(root, fn))
    return out


def _read_csv(path: str, raw: dict | None = None
              ) -> tuple[list[str], dict[str, list[float | None]]] | None:
    try:
        if os.path.getsize(path) > MAX_CSV_BYTES:
            return None
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            rd = csv.reader(f)
            hdr = next(rd, None)
            if not hdr:
                return None
            hdr = [h.strip() for h in hdr]
            cols: dict[str, list[float | None]] = {h: [] for h in hdr}
            strs: dict[str, list[str]] = {h: [] for h in hdr}
            for i, row in enumerate(rd):
                if i >= MAX_ROWS:
                    break
                if len(row) != len(hdr):
                    continue
                for h, v in zip(hdr, row):
                    cols[h].append(_num(v) if v.strip() else None)
                    strs[h].append(v.strip())
            if raw is not None:
                raw[path] = strs
    except (OSError, csv.Error):
        return None
    return hdr, cols


def _numeric(vals: list[float | None]) -> list[float]:
    return [v for v in vals if v is not None]


def _docs(run_dir: str) -> list[str]:
    """Documents whose quoted values describe the data. Critiques are
    excluded: they quote suspect values precisely to dispute them."""
    out = []
    for d in (run_dir, os.path.join(run_dir, "data")):
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".md") and not fn.startswith("critique_"):
                out.append(os.path.join(d, fn))
    return out


_DATA_DOCS = ("data_provenance.md", "data_quality.md")
# Column names that recur across unrelated tables: a quoted "value = 0.5"
# says nothing about any particular column.
_GENERIC_COLS = {"value", "mean", "median", "ci_low", "ci_high", "lower", "upper",
                 "estimate", "count", "total", "population", "year", "rate",
                 "score", "weight", "share", "index", "cost", "n_obs"}


def _data_docs(run_dir: str) -> list[str]:
    return [p for p in _docs(run_dir)
            if os.path.basename(p) in _DATA_DOCS or
            os.path.basename(os.path.dirname(p)) == "data"]


def _is_data_csv(run_dir: str, p: str) -> bool:
    return _rel(run_dir, p).split(os.sep)[0] == "data"


def _specific_col(h: str) -> bool:
    return len(h) >= 6 and "_" in h and h.lower() not in _GENERIC_COLS


def _rel(run_dir: str, p: str) -> str:
    return os.path.relpath(p, run_dir)


# --------------------------------------------------------------------------- #
# Unit checks
# --------------------------------------------------------------------------- #

def _power_of_ten(ratio: float, tol: float = 0.15) -> int | None:
    """k if ratio ≈ 10^k with |k| >= 2, else None."""
    if ratio <= 0:
        return None
    k = round(math.log10(ratio))
    if abs(k) >= 2 and abs(ratio / 10 ** k - 1) <= tol:
        return k
    return None


def _brackets(x, lo, hi) -> bool:
    eps = 1e-9 * max(1.0, abs(x))
    return (lo is None or lo <= x + eps) and (hi is None or hi >= x - eps)


def check_bounds(run_dir: str, tables: dict) -> list[dict]:
    out = []
    for path, (hdr, cols) in tables.items():
        hs = set(hdr)
        for base in hdr:
            for lo_s, hi_s in _BOUND_SUFFIXES:
                lo_c, hi_c = base + lo_s, base + hi_s
                if lo_c not in hs and hi_c not in hs:
                    continue
                rows = []
                for i, x in enumerate(cols[base]):
                    lo = cols[lo_c][i] if lo_c in hs else None
                    hi = cols[hi_c][i] if hi_c in hs else None
                    if x is not None and (lo is not None or hi is not None):
                        rows.append((x, lo, hi))
                bad = sum(not _brackets(*r) for r in rows)
                if len(rows) < 3 or bad / len(rows) < 0.2:
                    continue
                hint = ""
                names = (base, lo_c, hi_c)
                for j, k in ((j, k) for j in range(3) for k in (2, 3, -2, -3)):
                    fixed = [tuple(v / 10 ** k if v is not None and t == j else v
                                   for t, v in enumerate(r)) for r in rows]
                    if sum(_brackets(*r) for r in fixed) / len(rows) >= 0.9:
                        hint = (f" Dividing `{names[j]}` by 10^{k} restores the "
                                f"bracket, which suggests a unit change in that column.")
                        break
                shown = " / ".join(c for c in (lo_c, hi_c) if c in hs)
                out.append(_f(
                    "unit_bound_order_violated", "HIGH",
                    f"`{_rel(run_dir, path)}`: `{shown}` fail to bracket `{base}` on "
                    f"{bad}/{len(rows)} rows.{hint} Check the units of each column "
                    f"against the source.", _rel(run_dir, path)))
    return out


def _key_cols(hdr, cols, raw) -> dict[str, list[str]]:
    """Non-numeric columns whose values are mostly unique: join keys."""
    out = {}
    for h in hdr:
        vals = raw[h]
        if not vals or any(v is not None for v in cols[h][:50]):
            continue
        if len(set(vals)) >= 0.8 * len(vals) >= 4:
            out[h] = vals
    return out


def check_cross_file_scale(run_dir: str, tables: dict, raw: dict) -> list[dict]:
    """Same column name in two CSVs, rows joined on a shared key column:
    a matched-row ratio that is consistently a power of ten is a unit change
    (medians alone flag legitimately different scenarios)."""
    items = list(tables.items())
    keys = {p: _key_cols(h, c, raw[p]) for p, (h, c) in items}
    out = []
    for i, (p1, (h1, c1)) in enumerate(items):
        for p2, (h2, c2) in items[i + 1:]:
            shared = [h for h in h1 if h in set(h2) and _specific_col(h)
                      and h not in keys[p1] and h not in keys[p2]]
            if not shared:
                continue
            join = None
            for k in set(keys[p1]) & set(keys[p2]):
                idx2 = {v: j for j, v in enumerate(keys[p2][k])}
                pairs = [(j1, idx2[v]) for j1, v in enumerate(keys[p1][k]) if v in idx2]
                if len(pairs) >= 5:
                    join = (k, pairs)
                    break
            if join is None:
                continue
            k, pairs = join
            for col in shared:
                ratios = [c1[col][a] / c2[col][b] for a, b in pairs
                          if c1[col][a] and c2[col][b]]
                if len(ratios) < 5:
                    continue
                pk = _power_of_ten(statistics.median(ratios))
                if pk is None or sum(_power_of_ten(r) == pk for r in ratios) < 0.8 * len(ratios):
                    continue
                out.append(_f(
                    "unit_scale_mismatch", "HIGH",
                    f"Column `{col}` in `{_rel(run_dir, p1)}` is ~10^{pk}x the same "
                    f"column in `{_rel(run_dir, p2)}` on {len(ratios)} rows matched by "
                    f"`{k}`. The same name should carry the same unit; rescale one "
                    f"or rename it.", _rel(run_dir, p1)))
    return out


def check_doc_values(run_dir: str, tables: dict) -> list[dict]:
    colvals: dict[str, list[tuple[str, list[float]]]] = defaultdict(list)
    for path, (hdr, cols) in tables.items():
        if not _is_data_csv(run_dir, path):
            continue
        for h in hdr:
            v = _numeric(cols[h])
            if v and _specific_col(h):
                colvals[h].append((path, v))
    if not colvals:
        return []
    names = sorted(colvals, key=len, reverse=True)
    pat = re.compile(r"`?\b(" + "|".join(re.escape(n) for n in names) +
                     r")\b`?\s*(?:=|:|is|of)\s*(" + _NUM_RE + r")\s*(%?)")
    out = []
    flagged = set()
    for doc in _data_docs(run_dir):
        try:
            lines = open(doc, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            continue
        for ln, line in enumerate(lines, 1):
            for m in pat.finditer(line):
                col, q = m.group(1), _num(m.group(2))
                if q is None or q == 0 or col in flagged:
                    continue
                decimals = len(m.group(2).split(".")[1]) if "." in m.group(2) else 0
                tol = max(0.5 * 10 ** -decimals, abs(q) * 0.01)
                for path, vals in colvals[col]:
                    if any(abs(v - q) <= tol for v in vals):
                        break
                    scaled = [k for k in (2, 3, -2, -3)
                              if any(abs(v / 10 ** k - q) <= tol for v in vals)]
                    if scaled:
                        k = scaled[0]
                        flagged.add(col)
                        out.append(_f(
                            "unit_doc_scale_mismatch", "HIGH",
                            f"`{_rel(run_dir, doc)}:{ln}` quotes `{col}` = "
                            f"{m.group(2)}{m.group(3)}, but `{_rel(run_dir, path)}` "
                            f"holds that value only at 10^{k}x. One of them has the "
                            f"wrong unit.", _rel(run_dir, path)))
                        break
    return out


def check_ranges(run_dir: str, tables: dict) -> list[dict]:
    out = []
    for path, (hdr, cols) in tables.items():
        for h in hdr:
            v = _numeric(cols[h])
            if len(v) < 3:
                continue
            toks = set(re.split(r"[^a-z]+", h.lower()))
            mx = max(v)
            if toks & _PROPORTION_TOKENS and mx > 100:
                out.append(_f(
                    "unit_out_of_range", "MEDIUM",
                    f"`{_rel(run_dir, path)}` column `{h}` reads as a proportion "
                    f"but reaches {mx:.4g} (> 100). Check its unit, or rename it "
                    f"if it holds a count derived from a share.",
                    _rel(run_dir, path)))
                continue
            m = _PER_N_RE.search(h)
            if m:
                n = float(m.group(1)) * _PER_N_MULT[(m.group(2) or "").lower() or None]
                if n >= 100 and mx > n:
                    out.append(_f(
                        "unit_out_of_range", "MEDIUM",
                        f"`{_rel(run_dir, path)}` column `{h}` is per {n:g} but "
                        f"reaches {mx:.4g}. Check its unit.", _rel(run_dir, path)))
    return out


# --------------------------------------------------------------------------- #
# Vintage check
# --------------------------------------------------------------------------- #

def check_vintage(run_dir: str, csv_paths: list[str]) -> list[dict]:
    import datetime
    this_year = datetime.date.today().year
    data_docs = set(_data_docs(run_dir))
    mentions: dict[str, dict[int, list[tuple[str, bool]]]] = defaultdict(lambda: defaultdict(list))
    file_years: dict[str, set[int]] = defaultdict(set)
    for p in csv_paths + _walk(os.path.join(run_dir, "data"), ""):
        for m in _FILE_YEAR_RE.finditer(os.path.basename(p).lower()):
            file_years[m.group(1)].add(int(m.group(2)))
    for doc in _docs(run_dir):
        try:
            lines = open(doc, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            continue
        for ln, line in enumerate(lines, 1):
            if "http" in line:
                continue
            found = [(m.group(1), int(m.group(2))) for m in _YEAR_MENTION_RE.finditer(line)
                     if m.group(1) not in _YEAR_STOP and int(m.group(2)) <= this_year]
            per_name = defaultdict(set)
            for name, y in found:
                per_name[name].add(y)
            for name, y in found:
                if len(per_name[name]) >= 3:
                    continue  # a list of rounds on one line
                mentions[name.lower()][y].append(
                    (f"{_rel(run_dir, doc)}:{ln} \"{line.strip()[:140]}\"", doc in data_docs))
    out = []
    for name, years in sorted(mentions.items()):
        if len(years) < 2:
            continue
        for y1, where in sorted(years.items()):
            if len(where) != 1 or not where[0][1] or y1 in file_years.get(name, set()):
                continue
            rivals = [y for y, w in years.items()
                      if y != y1 and len(w) >= 2 and abs(y - y1) >= 5]
            if not rivals:
                continue
            y2 = max(rivals, key=lambda y: len(years[y]))
            loc = where[0][0]
            out.append(_f(
                "data_vintage_conflict", "MEDIUM",
                f"`{name.upper()}` is dated {y1} once ({loc}) but {y2} in "
                f"{len(years[y2])} other places. If {y1} is a typo the data's "
                f"stated vintage is wrong; if it is a different round, say so.",
                loc.split(":")[0]))
    return out


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #

def check_sources(run_dir: str) -> list[dict]:
    csv_paths = _walk(run_dir, ".csv")[:MAX_CSVS]
    tables, raw = {}, {}
    for p in csv_paths:
        t = _read_csv(p, raw)
        if t:
            tables[p] = t
    out = []
    out += check_bounds(run_dir, tables)
    out += check_cross_file_scale(run_dir, tables, raw)
    out += check_doc_values(run_dir, tables)
    out += check_ranges(run_dir, tables)
    out += check_vintage(run_dir, csv_paths)
    return out


def _self_test() -> int:
    import tempfile
    fails: list[str] = []

    def ok(c, m):
        if not c:
            fails.append(m)

    def kinds(fs):
        return sorted(f["kind"] for f in fs)

    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "data", "processed"))
        os.makedirs(os.path.join(d, "models"))
        w = lambda rel, s: open(os.path.join(d, rel), "w").write(s)
        bounds_ok = ("unit,target_rate,target_rate_lo,target_rate_hi\nA,25.5,20.1,30.2\n"
                     "B,31.0,27.0,35.0\nC,12.2,9.0,15.5\nD,40.0,35.5,44.1\n")
        share = lambda f: "unit,cov_share\n" + "".join(f"U{i},{f(i)}\n" for i in range(10))
        prov_ok = ("# Sources\n- PHS 2021 report; PHS 2021 tables.\n"
                   "- Unit B: target_rate = 31.0, consistent with the PHS 2021 final report.\n")
        # S1: clean run (bounds bracket, same scale, doc matches data) is silent
        w("data/targets.csv", bounds_ok)
        w("data/processed/master.csv", share(lambda i: f"{0.2 + i / 100:.3f}"))
        w("models/table.csv", share(lambda i: f"{0.2 + i / 100:.3f}"))
        w("data_provenance.md", prov_ok)
        ok(check_sources(d) == [], f"S1 clean run silent: {check_sources(d)}")

        # S2: lo column x100 → bound order violated, names the column to rescale
        w("data/targets.csv", "unit,target_rate,target_rate_lo,target_rate_hi\nA,25.5,2010,30.2\n"
          "B,31.0,2700,35.0\nC,12.2,900,15.5\nD,40.0,3550,44.1\n")
        fs = [f for f in check_sources(d) if f["kind"] == "unit_bound_order_violated"]
        ok(len(fs) == 1 and "`target_rate_lo` by 10^2" in fs[0]["claim"], f"S2 bounds: {fs}")
        w("data/targets.csv", bounds_ok)

        # S3: same column x100 on rows matched by key → scale mismatch
        w("models/table.csv", share(lambda i: f"{20 + i:.1f}"))
        ok(kinds(check_sources(d)) == ["unit_scale_mismatch"], f"S3 cross-file scale: {check_sources(d)}")
        # S3b: no shared key → no comparison (scenario tables legitimately differ)
        w("models/table.csv", "row,cov_share\n" + "".join(f"{i},{20 + i:.1f}\n" for i in range(10)))
        ok(check_sources(d) == [], f"S3b unjoinable tables silent: {check_sources(d)}")
        w("models/table.csv", share(lambda i: f"{0.2 + i / 100:.3f}"))

        # S4: data doc quotes target_rate = 31.0, data holds 3100 → doc scale mismatch
        w("data/targets.csv", "unit,target_rate\nA,2550\nB,3100\nC,1220\nD,4000\n")
        ok(kinds(check_sources(d)) == ["unit_doc_scale_mismatch"], f"S4 doc vs data: {check_sources(d)}")
        # S4b: value absent (other row or year) → silent
        w("data/targets.csv", "unit,target_rate\nA,25.5\nB,33.3\nC,12.2\nD,40.0\n")
        ok(check_sources(d) == [], "S4b absent value is not a unit error")
        # S4c: generic names ("value = 0.53") and non-data docs are ignored
        w("data/targets.csv", "unit,value\nA,53\nB,65\nC,21\n")
        w("citations.md", "value = 0.53\n")
        w("data_provenance.md", "value = 0.65\n")
        ok(check_sources(d) == [], f"S4c generic columns ignored: {check_sources(d)}")
        w("data/targets.csv", bounds_ok); w("data_provenance.md", prov_ok)

        # S5: proportion-named column > 100 → HIGH; per-1000 above 1000 → MEDIUM;
        # "prev_" (previous) is not a proportion
        w("data/processed/range.csv", "unit,coverage_pct,cases_per_1000,prev_objective\n"
          "A,4500,20,9000\nB,3000,1500,8000\nC,2000,30,7000\n")
        fs = [f for f in check_sources(d) if f["kind"] == "unit_out_of_range"]
        ok(len(fs) == 2 and all(f["severity"] == "MEDIUM" for f in fs), f"S5 ranges: {fs}")
        os.remove(os.path.join(d, "data/processed/range.csv"))

        # S6: a source dated 2007 once in a data doc while 2021 twice → conflict
        w("data_provenance.md", "# Sources\n- PHS 2021 report; PHS 2021 tables.\n"
          "- Values transcribed from PHS 2007 Table 4.1.\n")
        fs = check_vintage(d, [])
        ok(kinds(fs) == ["data_vintage_conflict"] and "PHS` is dated 2007" in fs[0]["claim"], f"S6 vintage: {fs}")
        # S6b: the odd year is carried by a data file → a real second round
        w("data/phs2007_extract.csv", "a,b\n1,2\n")
        ok(check_vintage(d, []) == [], f"S6b file-backed round silent: {check_vintage(d, [])}")
        os.remove(os.path.join(d, "data/phs2007_extract.csv"))
        # S6c: ranges, lists of rounds, author-year, currency years, odd mention
        # outside a data doc → silent
        w("data_provenance.md", "PHS 2021 and PHS 2021. Coverage PHS 2000-2014; PHS 2010/15/18;\n"
          "rounds PHS 2008, PHS 2013, PHS 2018. Smith 2004 vs Smith 2018 Smith 2018. USD 2010 prices.\n")
        w("plan.md", "PHS 2009 appears once here.\n")
        ok(check_vintage(d, []) == [], f"S6c non-vintage mentions ignored: {check_vintage(d, [])}")
        os.remove(os.path.join(d, "plan.md"))
        # S7: critiques are not treated as data documentation
        w("data_provenance.md", prov_ok)
        w("critique_domain.md", "target_rate = 3100 is wrong; PHS 2007 is stale\n")
        ok(check_sources(d) == [], "S7 critiques excluded")

    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir required")
    fs = check_sources(a.run_dir)
    print(json.dumps(fs, indent=1))
    return 1 if any(f["severity"] == "HIGH" for f in fs) else 0


if __name__ == "__main__":
    sys.exit(main())
