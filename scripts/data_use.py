#!/usr/bin/env python3
"""Phase 42 γ (D3): data-use audit. Every column of a processed data CSV is
read by model code, or listed as not read with a reason.

    python3 scripts/data_use.py <run_dir>
    python3 scripts/data_use.py --self-test

E7 attempt 8 had a per-unit data column set that described exactly the
variation the model collapsed; no model code read it, and nothing noticed.
This is a static scan, disease-agnostic:

* Data: every CSV under data/ except data/raw/ (the files data_schema.py
  checks), header row only.
* Code: every .py file under models/ (recursively, no __pycache__). A column
  counts as read when it appears in code as
    - a string literal equal to the column name, or a short code-like string
      containing it as a token (`"a,b"`, `"x > 0 and y < 1"`; strings with
      prose, docstrings and comments do not count);
    - an attribute or keyword name (`row.col`, `df.assign(col=...)`);
    - a match of an f-string or a prefix/suffix literal used to build names
      (`f"rate_{year}"` covers rate_2020; `"rate_" + str(y)` covers the
      same; a literal must be at least 4 characters and end or start with
      `_` to count as a prefix or suffix).
  A file that does not parse is scanned as text (word-boundary match).
* Exemptions: `models/data_use.yaml`

      not_read_by_model:
        - file: data/processed/units.csv
          columns: [hist_2000, hist_2005]     # or ["*"] for the whole file
          reason: "historical context, cited in the report only"

Findings (one per data file, never per column, to keep the gate readable):
  data_column_unused     MEDIUM  columns no model code reads and no reason given
  data_use_invalid       MEDIUM  data_use.yaml unreadable / malformed entry
  data_use_unreadable    MEDIUM  a data CSV whose header cannot be read
Nothing runs until models/ holds at least one .py file (no model yet).
"""
from __future__ import annotations

import argparse
import ast
import csv
import os
import re
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
EXEMPT_PATH = os.path.join("models", "data_use.yaml")
_LIST_CAP = 20
_CODE_STR_MAX = 400


def _sibling(name: str):
    """scripts/<name>.py by path, under a private module name, so a run's own
    module of the same name (models/ on sys.path) is never picked up."""
    import importlib.util
    key = f"_ma_p42_{name}"
    mod = sys.modules.get(key)
    if mod is None:
        spec = importlib.util.spec_from_file_location(key, os.path.join(HERE, f"{name}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(key, None)
            raise
    return mod


def data_csvs(run_dir: str, include_raw: bool = False) -> list[str]:
    return _sibling("data_schema").data_csvs(run_dir, include_raw=include_raw)


def read_header(path: str) -> list[str]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        row = next(csv.reader(f), None)
    if row is None:
        raise ValueError("empty file")
    return [c.strip() for c in row if c.strip()]


def processed_headers(run_dir: str, include_raw: bool = False) -> tuple[dict[str, list[str]], dict[str, str]]:
    """({rel_path: columns}, {rel_path: error}) for the run's processed data
    CSVs (and data/raw/ ones with include_raw)."""
    cols, errs = {}, {}
    for rel in data_csvs(run_dir, include_raw):
        try:
            cols[rel] = read_header(os.path.join(run_dir, rel))
        except (OSError, UnicodeDecodeError, ValueError, csv.Error) as e:
            errs[rel] = str(e)
    return cols, errs


def code_files(run_dir: str) -> list[str]:
    root = os.path.join(run_dir, "models")
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__" and not x.startswith("."))
        out += [os.path.join(d, f) for f in sorted(files) if f.endswith(".py")]
    return out


class _Refs:
    def __init__(self):
        self.exact: set[str] = set()      # literals, attributes, keywords
        self.code_strings: list[str] = []  # short code-like strings
        self.patterns: list[re.Pattern] = []
        self.prefixes: set[str] = set()
        self.suffixes: set[str] = set()
        self.raw_texts: list[str] = []     # unparseable files

    def reads(self, col: str) -> bool:
        if col in self.exact:
            return True
        tok = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(col) + r"(?![A-Za-z0-9_])")
        if any(tok.search(s) for s in self.code_strings):
            return True
        if any(p.fullmatch(col) for p in self.patterns):
            return True
        if any(col.startswith(p) and len(col) > len(p) for p in self.prefixes):
            return True
        if any(col.endswith(s) and len(col) > len(s) for s in self.suffixes):
            return True
        return any(tok.search(t) for t in self.raw_texts)


def _is_code_like(s: str) -> bool:
    """Short strings that name columns: no sentence punctuation, few words."""
    if len(s) > _CODE_STR_MAX or "\n" in s:
        return False
    words = re.findall(r"[A-Za-z]{3,}", s)
    # A sentence has several plain words and no column-ish separators.
    if len(words) > 6 and not re.search(r"[,_=<>\[\]]", s):
        return False
    return not re.search(r"[.!?:]\s+[A-Z]", s)


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    for node in ast.walk(tree):   # bare string statements are comments too
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            ids.add(id(node.value))
    return ids


def _add_literal(refs: _Refs, s: str) -> None:
    s_ = s.strip()
    if not s_:
        return
    refs.exact.add(s_)
    if _is_code_like(s_):
        refs.code_strings.append(s_)
    if len(s_) >= 4 and re.fullmatch(r"[A-Za-z0-9_]+", s_):
        if s_.endswith("_"):
            refs.prefixes.add(s_)
        if s_.startswith("_"):
            refs.suffixes.add(s_)


def collect_refs(run_dir: str) -> _Refs:
    refs = _Refs()
    for path in code_files(run_dir):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            refs.raw_texts.append(text)
            continue
        skip = _docstring_ids(tree)
        fstring_parts: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                parts, lit = [], []
                for v in node.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        fstring_parts.add(id(v))
                        parts.append(re.escape(v.value))
                        lit.append(v.value)
                    else:
                        parts.append(r"[A-Za-z0-9_.\-]+")
                if lit and len("".join(lit)) >= 3 and len(node.values) <= 6:
                    try:
                        refs.patterns.append(re.compile("".join(parts)))
                    except re.error:
                        pass
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in skip or id(node) in fstring_parts:
                    continue
                _add_literal(refs, node.value)
            elif isinstance(node, ast.Attribute):
                refs.exact.add(node.attr)
            elif isinstance(node, ast.keyword) and node.arg:
                refs.exact.add(node.arg)
    return refs


def _load_exemptions(run_dir: str) -> tuple[dict[str, set[str]], list[str]]:
    path = os.path.join(run_dir, EXEMPT_PATH)
    if not os.path.exists(path):
        return {}, []
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        return {}, [f"unreadable: {e}"]
    if not isinstance(doc, dict) or not isinstance(doc.get("not_read_by_model"), list):
        return {}, ["top level must be a mapping with a `not_read_by_model:` list"]
    out: dict[str, set[str]] = {}
    errs = []
    for i, e in enumerate(doc["not_read_by_model"]):
        if not isinstance(e, dict):
            errs.append(f"entry {i} is not a mapping")
            continue
        f_, cols, why = e.get("file"), e.get("columns"), e.get("reason")
        if not isinstance(f_, str) or not f_.strip():
            errs.append(f"entry {i} has no `file:`")
            continue
        if not isinstance(why, str) or len(why.strip()) < 10:
            errs.append(f"entry {i} ({f_}) has no `reason:` (at least a short sentence)")
            continue
        if not isinstance(cols, list) or not cols or not all(isinstance(c, str) for c in cols):
            errs.append(f"entry {i} ({f_}) needs `columns:` as a non-empty list (or [\"*\"])")
            continue
        out.setdefault(os.path.normpath(f_.strip()), set()).update(c.strip() for c in cols)
    return out, errs


def audit(run_dir: str) -> dict:
    """{'unused': {file: [cols]}, 'read': n, 'total': n, 'errors': [..], 'exempt': n}"""
    res = {"unused": {}, "read": 0, "total": 0, "exempt": 0, "errors": [], "unreadable": {}, "skipped": False}
    if not code_files(run_dir):
        res["skipped"] = True
        return res
    headers, res["unreadable"] = processed_headers(run_dir)
    exempt, res["errors"] = _load_exemptions(run_dir)
    refs = collect_refs(run_dir)
    for rel, cols in headers.items():
        ex = exempt.get(os.path.normpath(rel), set())
        for c in cols:
            res["total"] += 1
            if "*" in ex or c in ex:
                res["exempt"] += 1
            elif refs.reads(c):
                res["read"] += 1
            else:
                res["unused"].setdefault(rel, []).append(c)
    return res


def gate_findings(run_dir: str) -> list[dict]:
    res = audit(run_dir)
    out = []
    for e in res["errors"]:
        out.append({"kind": "data_use_invalid", "severity": "MEDIUM", "stage": "MODEL", "target_stage": "MODEL",
                    "scope_declarable": False, "claim": f"{EXEMPT_PATH}: {e}"})
    for rel, err in sorted(res["unreadable"].items()):
        out.append({"kind": "data_use_unreadable", "severity": "MEDIUM", "stage": "DATA", "target_stage": "DATA",
                    "scope_declarable": False,
                    "claim": f"`{rel}`: the header cannot be read ({err[:120]}), so the data-use audit cannot check it."})
    for rel, cols in sorted(res["unused"].items()):
        shown = ", ".join(f"`{c}`" for c in cols[:_LIST_CAP]) + (f" and {len(cols) - _LIST_CAP} more"
                                                               if len(cols) > _LIST_CAP else "")
        out.append({
            "kind": "data_column_unused", "severity": "MEDIUM", "stage": "MODEL", "target_stage": "MODEL",
            "scope_declarable": True, "n_columns": len(cols),
            "claim": (f"`{rel}`: {len(cols)} column(s) that no code under models/ reads: {shown}. Data the run "
                      "assembled but the model ignores may hold variation the model collapses. Either read each "
                      f"column in the model (or a sufficiency test), or list it in `{EXEMPT_PATH}` under "
                      "`not_read_by_model:` with a reason.")})
    return out


# --------------------------------------------------------------------------- self-test

def _self_test() -> int:
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    def w(d, rel, text):
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    with tempfile.TemporaryDirectory() as d:
        w(d, "data/processed/units.csv",
          "unit,pop,rate_2020,rate_2021,mix_a,mix_b,lat,score_hist,state\nA,1,2,3,4,5,6,7,8\n")
        w(d, "data/raw/raw.csv", "ignored_col\n1\n")
        ok(gate_findings(d) == [], f"DU0 no model code yet → nothing, got {gate_findings(d)}")
        w(d, "models/model.py", '''"""Model reads unit and pop. mix_a is described in this docstring only."""
import csv
YEARS = (2020, 2021)
def load(path):
    rows = list(csv.DictReader(open(path)))
    # mix_b in a comment does not count
    for r in rows:
        r["pop"] = float(r["pop"])
        rates = [float(r[f"rate_{y}"]) for y in YEARS]
    print("Loaded the state table for the report and checked the score history today.")
    return {r["unit"]: r for r in rows}
''')
        v = gate_findings(d)
        un = audit(d)["unused"].get("data/processed/units.csv", [])
        ok(len(v) == 1 and v[0]["kind"] == "data_column_unused" and v[0]["severity"] == "MEDIUM",
           f"DU1 one MEDIUM finding per file, got {v}")
        ok(sorted(un) == ["lat", "mix_a", "mix_b", "score_hist", "state"],
           f"DU2 docstring / comment / prose mentions do not count, f-strings do, got {un}")
        ok("ignored_col" not in str(v), "DU3 data/raw is not audited")
        w(d, "models/extra.py", 'COLS = "lat,state"\nx = df.mix_a\ndf.assign(mix_b=1)\nP = "score_" + "x"\n')
        ok(audit(d)["unused"] == {}, f"DU4 code-like lists, attributes, keywords and prefixes count, "
                                     f"got {audit(d)['unused']}")
        os.remove(os.path.join(d, "models", "extra.py"))
        w(d, "models/data_use.yaml", "not_read_by_model:\n  - file: data/processed/units.csv\n"
                                     "    columns: [lat, state, mix_a, mix_b]\n"
                                     "    reason: identifiers and reference columns for the report maps\n")
        ok(audit(d)["unused"] == {"data/processed/units.csv": ["score_hist"]},
           f"DU5 exempted columns with a reason are not flagged, got {audit(d)['unused']}")
        w(d, "models/data_use.yaml", "not_read_by_model:\n  - file: data/processed/units.csv\n"
                                     "    columns: ['*']\n    reason: ''\n")
        v = gate_findings(d)
        ok(any(x["kind"] == "data_use_invalid" for x in v) and any(x["kind"] == "data_column_unused" for x in v),
           f"DU6 an exemption without a reason is invalid and exempts nothing, got {v}")
        w(d, "models/data_use.yaml", "not_read_by_model: [")
        ok(any(x["kind"] == "data_use_invalid" for x in gate_findings(d)), "DU7 unparseable data_use.yaml fails closed")
        os.remove(os.path.join(d, "models", "data_use.yaml"))
        w(d, "models/broken.py", "def f(:\n    return r['score_hist']\n")
        ok("score_hist" not in audit(d)["unused"].get("data/processed/units.csv", []),
           "DU8 an unparseable file is scanned as text")
        w(d, "data/bad.csv", "")
        ok(any(x["kind"] == "data_use_unreadable" for x in gate_findings(d)), "DU9 an empty CSV fails closed")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir required")
    res = audit(a.run_dir)
    if res["skipped"]:
        print("no .py files under models/: nothing to audit")
        return 0
    n_un = sum(len(v) for v in res["unused"].values())
    print(f"columns: {res['total']}  read: {res['read']}  exempt: {res['exempt']}  unused: {n_un}")
    for rel, cols in sorted(res["unused"].items()):
        print(f"  {rel}: {len(cols)} unused: {', '.join(cols)}")
    for e in res["errors"]:
        print("ERROR:", e)
    return 1 if n_un or res["errors"] or res["unreadable"] else 0


if __name__ == "__main__":
    sys.exit(main())
