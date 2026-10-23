#!/usr/bin/env python3
"""
Phase 22 δ — declared data schema: units and ranges stated once, at the source.

E5/E5b: a column multiplied by 100 (a fraction turned percent, a rate per
1,000 turned per 100,000) was caught by no critic and, without a reference,
by no heuristic either: a ×100 change is invisible unless something says
what the unit should be. So the data agent now says it. For every data CSV
it writes, `data/data_schema.yaml` declares each column's kind, unit and
plausible range, and this script checks the data against the declaration.
The modeler reads the same file, so units are explicit where they are used.

Schema:

    files:
      data/processed/unit_table.csv:
        columns:
          unit_name:   {kind: id}
          prevalence:  {kind: proportion, unit: "fraction, all ages", range: [0, 0.9],
                        source: "national survey, 2021"}
          incidence:   {kind: rate, unit: "cases per 1,000 person-years", per: 1000,
                        range: [0, 800]}
          population:  {kind: count, unit: persons, range: [1000, 2.0e7]}

Kinds: proportion (range within [0, 1]), percent (within [0, 100]), rate
(needs `per`), count (>= 0), currency, continuous, year, id, categorical.
Numeric kinds need `range` — the plausible range for this quantity from its
source, not the widest legal one; that is what catches a unit slip.

Findings (validator `_check_data_schema`):
  data_schema_missing          MEDIUM @ r2, HIGH @ r>=3 — data CSVs exist, no schema
  data_schema_invalid          HIGH  unparseable; unknown kind; numeric kind without
                                     range; range outside what the kind allows
  data_schema_out_of_range     HIGH  declared column has values outside its range
  data_schema_unit_conflict    HIGH  same column name declared with different
                                     kind/unit/per in two files
  data_schema_undeclared       MEDIUM processed/top-level data CSVs or columns
                                     with no declaration (one finding per file)
  data_schema_stale_entry      MEDIUM declared file or column that does not exist

Files under data/raw/ need no declaration (they are source downloads), but
are checked when declared. Everything here is disease-agnostic.

Usage:
    python3 scripts/data_schema.py <run_dir>
    python3 scripts/data_schema.py --self-test
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys

import yaml

SCHEMA_REL = os.path.join("data", "data_schema.yaml")
MAX_ROWS = 200000
MAX_BYTES = 200 * 1024 * 1024
NUMERIC_KINDS = {"proportion", "percent", "rate", "count", "currency", "continuous", "year"}
KINDS = NUMERIC_KINDS | {"id", "categorical"}
_KIND_BOUNDS = {"proportion": (0.0, 1.0), "percent": (0.0, 100.0), "count": (0.0, math.inf),
                "rate": (0.0, math.inf)}


def _f(kind: str, sev: str, claim: str, file: str = "") -> dict:
    out = {"kind": kind, "severity": sev, "stage": "DATA", "claim": claim}
    if sev == "HIGH":
        out["scope_declarable"] = False
    if file:
        out["file"] = file
    return out


def data_csvs(run_dir: str, include_raw: bool = False) -> list[str]:
    out = []
    root = os.path.join(run_dir, "data")
    for d, dirs, files in os.walk(root):
        rel_d = os.path.relpath(d, run_dir)
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x != "__pycache__"
                         and (include_raw or os.path.join(rel_d, x) != os.path.join("data", "raw")))
        out += [os.path.relpath(os.path.join(d, f), run_dir) for f in sorted(files)
                if f.lower().endswith(".csv")]
    return out


def _num(v: str):
    try:
        x = float(v.replace(",", ""))
    except (ValueError, AttributeError):
        return None
    return x if math.isfinite(x) else None


def _read(path: str):
    if os.path.getsize(path) > MAX_BYTES:
        return None
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        rd = csv.reader(f)
        hdr = [h.strip() for h in (next(rd, None) or [])]
        cols = {h: [] for h in hdr}
        for i, row in enumerate(rd):
            if i >= MAX_ROWS:
                break
            if len(row) == len(hdr):
                for h, v in zip(hdr, row):
                    cols[h].append(v.strip())
    return hdr, cols


def _validate_decl(fname: str, col: str, d) -> list[str]:
    if not isinstance(d, dict):
        return [f"{fname}::{col} must be a mapping"]
    kind = d.get("kind")
    if kind not in KINDS:
        return [f"{fname}::{col} kind {kind!r} not in {sorted(KINDS)}"]
    errs = []
    if kind in NUMERIC_KINDS:
        r = d.get("range")
        if not (isinstance(r, (list, tuple)) and len(r) == 2
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in r) and r[0] <= r[1]):
            errs.append(f"{fname}::{col} ({kind}) needs `range: [lo, hi]` with lo <= hi")
        elif kind in _KIND_BOUNDS:
            lo, hi = _KIND_BOUNDS[kind]
            if r[0] < lo or r[1] > hi:
                errs.append(f"{fname}::{col} range {list(r)} is outside what `{kind}` allows "
                            f"[{lo:g}, {hi:g}]")
        if not d.get("unit"):
            errs.append(f"{fname}::{col} ({kind}) needs `unit`")
    if kind == "rate" and not isinstance(d.get("per"), (int, float)):
        errs.append(f"{fname}::{col} (rate) needs numeric `per` (e.g. 1000)")
    return errs


def check(run_dir: str, round_n: int | None = None) -> list[dict]:
    path = os.path.join(run_dir, SCHEMA_REL)
    required = data_csvs(run_dir)
    if not os.path.exists(path):
        if not required or round_n is None or round_n < 2:
            return []
        sev = "HIGH" if round_n >= 3 else "MEDIUM"
        return [_f("data_schema_missing", sev,
                   f"{len(required)} data CSV(s) (e.g. `{required[0]}`) but no `{SCHEMA_REL}`. "
                   f"The data agent declares each column's kind, unit and plausible range "
                   f"there; see the data agent prompt.", SCHEMA_REL)]
    try:
        doc = yaml.safe_load(open(path, encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        return [_f("data_schema_invalid", "HIGH", f"`{SCHEMA_REL}` could not be parsed: {e}", SCHEMA_REL)]
    files = doc.get("files") if isinstance(doc, dict) else None
    if not isinstance(files, dict):
        return [_f("data_schema_invalid", "HIGH", f"`{SCHEMA_REL}` needs a top-level `files:` mapping",
                   SCHEMA_REL)]

    out: list[dict] = []
    errs: list[str] = []
    seen_units: dict[str, list[tuple[str, tuple]]] = {}
    for fname, fdecl in sorted(files.items()):
        cols_decl = (fdecl or {}).get("columns") if isinstance(fdecl, dict) else None
        if not isinstance(cols_decl, dict):
            errs.append(f"{fname} needs a `columns:` mapping")
            continue
        fpath = os.path.join(run_dir, fname)
        if not os.path.isfile(fpath):
            out.append(_f("data_schema_stale_entry", "MEDIUM",
                          f"`{SCHEMA_REL}` declares `{fname}`, which does not exist.", SCHEMA_REL))
            continue
        t = _read(fpath)
        if t is None:
            continue
        hdr, cols = t
        good = {}
        for col, d in cols_decl.items():
            e = _validate_decl(fname, col, d)
            errs += e
            if not e:
                good[col] = d
                if d["kind"] in NUMERIC_KINDS:
                    seen_units.setdefault(col, []).append(
                        (fname, (d["kind"], str(d.get("unit")).strip().lower(), d.get("per"))))
        missing_cols = [c for c in cols_decl if c not in cols]
        if missing_cols:
            out.append(_f("data_schema_stale_entry", "MEDIUM",
                          f"`{fname}` has no column(s) {missing_cols} declared in `{SCHEMA_REL}`.", fname))
        undeclared = [h for h in hdr if h and h not in cols_decl]
        if undeclared and fname in required:
            out.append(_f("data_schema_undeclared", "MEDIUM",
                          f"`{fname}`: {len(undeclared)} column(s) without a declaration "
                          f"({undeclared[:8]}).", fname))
        for col, d in good.items():
            if d["kind"] not in NUMERIC_KINDS or col not in cols:
                continue
            lo, hi = d["range"]
            vals = [x for x in map(_num, cols[col]) if x is not None]
            bad = [x for x in vals if x < lo or x > hi]
            nonnum = sum(1 for v in cols[col] if v and _num(v) is None)
            if bad:
                out.append(_f("data_schema_out_of_range", "HIGH",
                              f"`{fname}` column `{col}` ({d['kind']}, {d.get('unit')}): {len(bad)}/"
                              f"{len(vals)} values outside the declared range [{lo:g}, {hi:g}] "
                              f"(min {min(vals):.4g}, max {max(vals):.4g}). A unit change, a "
                              f"processing error, or a wrong declaration — fix whichever it is.",
                              fname))
            if nonnum:
                out.append(_f("data_schema_out_of_range", "HIGH",
                              f"`{fname}` column `{col}` is declared {d['kind']} but has "
                              f"{nonnum} non-numeric value(s).", fname))
    for fname in required:
        if fname not in files:
            out.append(_f("data_schema_undeclared", "MEDIUM",
                          f"`{fname}` is not declared in `{SCHEMA_REL}`.", fname))
    for col, decls in sorted(seen_units.items()):
        if len({u for _, u in decls}) > 1:
            out.append(_f("data_schema_unit_conflict", "HIGH",
                          f"Column `{col}` is declared with different kind/unit/per across files: "
                          + "; ".join(f"`{f}` {u}" for f, u in decls)
                          + ". One name must mean one unit; rename or convert.", decls[0][0]))
    out += [_f("data_schema_invalid", "HIGH", f"`{SCHEMA_REL}`: {e}", SCHEMA_REL) for e in errs]
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
        os.makedirs(os.path.join(d, "data", "raw"))
        w = lambda rel, s: open(os.path.join(d, rel), "w").write(s)
        w("data/processed/units.csv", "unit,prev,inc_per_1000,pop\nA,0.25,120,50000\nB,0.31,90,80000\n"
          "C,0.12,40,20000\n")
        w("data/raw/download.csv", "x\n1\n")
        schema = {"files": {"data/processed/units.csv": {"columns": {
            "unit": {"kind": "id"},
            "prev": {"kind": "proportion", "unit": "fraction", "range": [0, 0.9]},
            "inc_per_1000": {"kind": "rate", "unit": "cases per 1000 py", "per": 1000, "range": [0, 800]},
            "pop": {"kind": "count", "unit": "persons", "range": [1000, 1e7]}}}}}
        ok(check(d, 3) == [_f("data_schema_missing", "HIGH",
                              f"1 data CSV(s) (e.g. `data/processed/units.csv`) but no `{SCHEMA_REL}`. "
                              f"The data agent declares each column's kind, unit and plausible range "
                              f"there; see the data agent prompt.", SCHEMA_REL)], "D1 missing at r3 → HIGH")
        ok(check(d, 1) == [], "D1b missing at r1 silent (drafting window)")
        w(SCHEMA_REL, yaml.safe_dump(schema))
        ok(check(d, 3) == [], f"D2 declared, in range, raw undeclared → silent: {check(d, 3)}")
        # D3: the E5 unit plant — a fraction column ×100 → out of range
        w("data/processed/units.csv", "unit,prev,inc_per_1000,pop\nA,25,120,50000\nB,31,90,80000\nC,12,40,20000\n")
        ok(kinds(check(d, 3)) == ["data_schema_out_of_range"], f"D3 ×100 caught: {check(d, 3)}")
        # D4: a rate per 1000 turned per 100k → out of range
        w("data/processed/units.csv", "unit,prev,inc_per_1000,pop\nA,0.25,12000,50000\nB,0.31,9000,80000\n"
          "C,0.12,4000,20000\n")
        ok(kinds(check(d, 3)) == ["data_schema_out_of_range"], "D4 rate rescale caught")
        w("data/processed/units.csv", "unit,prev,inc_per_1000,pop\nA,0.25,120,50000\nB,0.31,90,80000\n"
          "C,0.12,40,20000\n")
        # D5: invalid declarations (proportion range beyond 1, rate without per, no range)
        bad = yaml.safe_load(yaml.safe_dump(schema))
        cols = bad["files"]["data/processed/units.csv"]["columns"]
        cols["prev"]["range"] = [0, 100]
        del cols["inc_per_1000"]["per"]
        del cols["pop"]["range"]
        w(SCHEMA_REL, yaml.safe_dump(bad))
        fs = check(d, 3)
        ok(kinds(fs).count("data_schema_invalid") == 3, f"D5 invalid declarations: {fs}")
        # D6: same column name, different unit in two files → conflict
        w("data/processed/other.csv", "unit,prev\nA,25\nB,31\n")
        both = yaml.safe_load(yaml.safe_dump(schema))
        both["files"]["data/processed/other.csv"] = {"columns": {
            "unit": {"kind": "id"}, "prev": {"kind": "percent", "unit": "percent", "range": [0, 90]}}}
        w(SCHEMA_REL, yaml.safe_dump(both))
        ok(kinds(check(d, 3)) == ["data_schema_unit_conflict"], f"D6 unit conflict: {check(d, 3)}")
        # D7: undeclared file and column, stale entry → MEDIUM each
        os.remove(os.path.join(d, "data/processed/other.csv"))
        w("data/processed/new.csv", "a,b\n1,2\n")
        w("data/processed/units.csv", "unit,prev,inc_per_1000,pop,extra\nA,0.25,120,50000,1\n")
        fs = check(d, 3)
        ok(sorted((f["kind"], f["severity"]) for f in fs) == [
            ("data_schema_stale_entry", "MEDIUM"), ("data_schema_undeclared", "MEDIUM"),
            ("data_schema_undeclared", "MEDIUM")], f"D7 undeclared/stale: {fs}")
        # D8: HIGH findings are not scope-declarable
        w("data/processed/units.csv", "unit,prev,inc_per_1000,pop\nA,25,120,50000\n")
        ok(all(f.get("scope_declarable") is False for f in check(d, 3) if f["severity"] == "HIGH"),
           "D8 HIGH non-declarable")

    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Check data CSVs against data/data_schema.yaml")
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--round", type=int, default=3)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir required")
    fs = check(a.run_dir, a.round)
    print(json.dumps(fs, indent=1))
    return 1 if any(f["severity"] == "HIGH" for f in fs) else 0


if __name__ == "__main__":
    sys.exit(main())
