#!/usr/bin/env python3
"""Phase 42 γ (D3): data-use audit. Processed data the model never reads is
reported, with weight where it carries signal.

    python3 scripts/data_use.py <run_dir>
    python3 scripts/data_use.py --self-test

E7 attempt 8 had a per-unit data column set that described exactly the
variation the model collapsed; no model code read it, and nothing noticed.
This is a static scan, disease-agnostic:

* Data: CSVs under data/processed/ (header row only); when a run has no
  data/processed/, the CSVs directly under data/ outside data/raw/.
  data/raw/ is the agents' working input and is scanned only for a required
  mechanism's trigger columns that exist nowhere else.
* Code: every .py file under models/ (recursively, no __pycache__). A file
  counts as opened when its file name appears in a code string (a literal or
  an f-string part). A column counts as read when it appears in code as
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

Findings:
  data_trigger_column_unused  MEDIUM  per column: a column named by the
                              `when_data_columns_any` of a mechanism a matched
                              prior requires (scripts/mechanism_manifest.py),
                              unread, unless the manifest records the mechanism
                              as simplified or fixed (D1 then checks its test)
  data_files_unused           MEDIUM  one per run: the processed data files no
                              model code opens (size, column count); each is
                              cleared by a `columns: ["*"]` exemption
  data_columns_unread_summary LOW     one per run: other unread columns in files
                              the model does read (counts per file, examples)
  data_use_invalid            MEDIUM  data_use.yaml unreadable / malformed entry
  data_use_unreadable         MEDIUM  a data CSV whose header cannot be read
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
_SUMMARY_FILES = 6
_SUMMARY_EXAMPLES = 4
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
        self.all_strings: list[str] = []   # every non-docstring literal / f-string part

    def opens(self, rel: str) -> bool:
        """The data file's name appears in a code string (or unparseable code)."""
        base = os.path.basename(rel)
        tok = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(base) + r"(?![A-Za-z0-9_])")
        return any(tok.search(s) for s in self.all_strings) or any(p.fullmatch(base) for p in self.patterns) \
            or any(tok.search(t) for t in self.raw_texts)

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
                        refs.all_strings.append(v.value)
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
                refs.all_strings.append(node.value)
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


def audited_csvs(run_dir: str) -> list[str]:
    """The modelling data: CSVs under data/processed/ when that directory has
    any; otherwise the CSVs directly under data/ outside data/raw/ (older
    layout). data/raw/ is the agents' working input, not a commitment."""
    allc = data_csvs(run_dir)
    proc = [r for r in allc if r.startswith(os.path.join("data", "processed") + os.sep)]
    return proc or allc


def _trigger_specs(run_dir: str, priors=None) -> tuple[list[tuple[str, object]], set[str]]:
    """([(prior_id, Mechanism)] with trigger columns, mechanism ids the manifest
    already accounts for as simplified or fixed)."""
    mm = _sibling("mechanism_manifest")
    req, _err = mm.required_mechanisms(run_dir, priors)   # the D1 check reports err
    entries, _f, _exists = mm._load_manifest(run_dir)
    accounted = {k for k, e in (entries or {}).items() if e.get("status") in ("simplified", "fixed")}
    return [(p, m) for p, m in req if m.when_data_columns_any], accounted


def audit(run_dir: str, priors=None) -> dict:
    """{'triggers': [{file, column, mechanism, prior}], 'unopened': [file],
        'unread': {file: [cols]} (opened files), 'total', 'read', 'exempt',
        'errors', 'unreadable', 'skipped'}"""
    res = {"triggers": [], "unopened": [], "ncols": {}, "unread": {}, "read": 0, "total": 0, "exempt": 0,
           "errors": [], "unreadable": {}, "skipped": False}
    if not code_files(run_dir):
        res["skipped"] = True
        return res
    exempt, res["errors"] = _load_exemptions(run_dir)
    refs = collect_refs(run_dir)
    headers = {}
    for rel in audited_csvs(run_dir):
        try:
            headers[rel] = read_header(os.path.join(run_dir, rel))
        except (OSError, UnicodeDecodeError, ValueError, csv.Error) as e:
            res["unreadable"][rel] = str(e)

    def exempted(rel, c):
        ex = exempt.get(os.path.normpath(rel), set())
        return "*" in ex or c in ex

    # (1) Columns a required mechanism's prior names, unread by the model.
    specs, accounted = _trigger_specs(run_dir, priors)
    flagged: set[tuple[str, str]] = set()
    raw_headers = None
    for pid, m in specs:
        if m.id in accounted:
            continue          # simplified (with its test) or fixed: D1 checks it
        subs = [s.lower() for s in m.when_data_columns_any]
        hits = [(rel, c) for rel, cs in headers.items() for c in cs if any(s in c.lower() for s in subs)]
        # Trigger columns that exist only under data/raw/ (by name), each one:
        # a column dropped in processing is variation the model cannot see.
        if raw_headers is None:
            raw_headers, _ = processed_headers(run_dir, include_raw=True)
        seen = {c for _r, c in hits}
        for rel, cs in raw_headers.items():
            if rel in headers:
                continue
            for c in cs:
                if c not in seen and any(s in c.lower() for s in subs):
                    seen.add(c)
                    hits.append((rel, c))
        for rel, c in hits:
            if (rel, c) in flagged or exempted(rel, c) or refs.reads(c):
                continue
            flagged.add((rel, c))
            res["triggers"].append({"file": rel, "column": c, "mechanism": m.id, "prior": pid})

    # (2) files the model never opens, (3) other unread columns.
    for rel, cols in headers.items():
        res["total"] += len(cols)
        res["ncols"][rel] = len(cols)
        if "*" in exempt.get(os.path.normpath(rel), set()):
            res["exempt"] += len(cols)
            continue
        if not refs.opens(rel):
            res["unopened"].append(rel)
            continue
        for c in cols:
            if exempted(rel, c):
                res["exempt"] += 1
            elif refs.reads(c):
                res["read"] += 1
            elif (rel, c) not in flagged:
                res["unread"].setdefault(rel, []).append(c)
    return res


def gate_findings(run_dir: str, priors=None) -> list[dict]:
    res = audit(run_dir, priors)
    out = []
    for e in res["errors"]:
        out.append({"kind": "data_use_invalid", "severity": "MEDIUM", "stage": "MODEL", "target_stage": "MODEL",
                    "scope_declarable": False, "claim": f"{EXEMPT_PATH}: {e}"})
    for rel, err in sorted(res["unreadable"].items()):
        out.append({"kind": "data_use_unreadable", "severity": "MEDIUM", "stage": "DATA", "target_stage": "DATA",
                    "scope_declarable": False,
                    "claim": f"`{rel}`: the header cannot be read ({err[:120]}), so the data-use audit cannot check it."})
    for t in res["triggers"]:
        out.append({
            "kind": "data_trigger_column_unused", "severity": "MEDIUM", "stage": "MODEL", "target_stage": "MODEL",
            "scope_declarable": True, "mechanism_id": t["mechanism"], "prior_id": t["prior"],
            "claim": (f"`{t['file']}` column `{t['column']}` is data for mechanism `{t['mechanism']}` (prior "
                      f"`{t['prior']}`), and no code under models/ reads it. Read it in the model, or record the "
                      f"mechanism as `simplified` with a sufficiency test in `models/mechanism_manifest.yaml`.")})
    if res["unopened"]:
        def _desc(rel):
            try:
                kb = f"{os.path.getsize(os.path.join(run_dir, rel)) / 1024:.0f} KB"
            except OSError:
                kb = "size unknown"
            return f"`{rel}` ({kb}, {res['ncols'].get(rel, 0)} columns)"
        files = sorted(res["unopened"])
        out.append({
            "kind": "data_files_unused", "severity": "MEDIUM", "stage": "MODEL", "target_stage": "MODEL",
            "scope_declarable": True, "files": files,
            "claim": (f"{len(files)} processed data file(s) that no code under models/ opens: "
                      f"{'; '.join(_desc(r) for r in files)}. Use each, or list it in `{EXEMPT_PATH}` "
                      f"(`columns: [\"*\"]`) with a reason.")})
    if res["unread"]:
        n = sum(len(v) for v in res["unread"].values())
        top = sorted(res["unread"].items(), key=lambda kv: -len(kv[1]))
        parts = [f"`{rel}` {len(cols)} ({', '.join(cols[:_SUMMARY_EXAMPLES])}"
                 f"{', ...' if len(cols) > _SUMMARY_EXAMPLES else ''})" for rel, cols in top[:_SUMMARY_FILES]]
        more = f"; and {len(top) - _SUMMARY_FILES} more file(s)" if len(top) > _SUMMARY_FILES else ""
        out.append({
            "kind": "data_columns_unread_summary", "severity": "LOW", "stage": "MODEL", "target_stage": "MODEL",
            "scope_declarable": True, "n_columns": n,
            "claim": (f"{n} column(s) in data files the model reads are not read by any code under models/: "
                      f"{'; '.join(parts)}{more}. Informational; check that none holds variation the model "
                      "collapses.")})
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

    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from lib import expert_priors as ep
    P = [ep._from_dict({
        "id": "widget_mix", "kind": "methodological_standard", "severity_if_unaddressed": "HIGH",
        "claim": "c", "rationale": "r", "applies_when": {"keywords_any": ["widget"], "domain_any": []},
        "requires_mechanism": [{"id": "mix_by_group", "description": "d", "sufficiency_test": "t",
                                "when_data_columns_any": ["mix_"]}]})]
    kinds = lambda v: sorted(x["kind"] for x in v)
    with tempfile.TemporaryDirectory() as d:
        w(d, "data/processed/units.csv",
          "unit,pop,rate_2020,rate_2021,mix_a,mix_b,lat,score_hist,state\nA,1,2,3,4,5,6,7,8\n")
        w(d, "data/processed/extra_table.csv", "unit,other\nA,1\n")
        w(d, "data/raw/raw.csv", "ignored_col\n1\n")
        ok(gate_findings(d, P) == [], f"DU0 no model code yet → nothing, got {gate_findings(d, P)}")
        w(d, "models/model.py", '''"""Model reads unit and pop. mix_a is described in this docstring only."""
import csv, os
DATA = os.path.join("data", "processed", "units.csv")
YEARS = (2020, 2021)
def load(path=DATA):
    rows = list(csv.DictReader(open(path)))
    # mix_b in a comment does not count
    for r in rows:
        r["pop"] = float(r["pop"])
        rates = [float(r[f"rate_{y}"]) for y in YEARS]
    print("Loaded the state table for the report and checked the score history today.")
    return {r["unit"]: r for r in rows}
''')
        v = gate_findings(d, P)
        un = audit(d, P)["unread"].get("data/processed/units.csv", [])
        ok(kinds(v) == ["data_columns_unread_summary", "data_files_unused"],
           f"DU1 no matched prior: unopened files are one MEDIUM, other unread columns one LOW summary, got {v}")
        w(d, "data/processed/third_table.csv", "unit,z\nA,1\n")
        v = gate_findings(d, P)
        agg = [x for x in v if x["kind"] == "data_files_unused"]
        ok(len(agg) == 1 and agg[0]["files"] == ["data/processed/extra_table.csv", "data/processed/third_table.csv"]
           and "2 columns" in agg[0]["claim"],
           f"DU1b all unopened files in a single MEDIUM per run, with their column counts, got {agg}")
        os.remove(os.path.join(d, "data", "processed", "third_table.csv"))
        ok(sorted(un) == ["lat", "mix_a", "mix_b", "score_hist", "state"],
           f"DU2 docstring / comment / prose mentions do not count, f-strings do, got {un}")
        ok("ignored_col" not in str(v) and "raw.csv" not in str(v), "DU3 data/raw is not audited")
        ok(next(x for x in v if x["kind"] == "data_columns_unread_summary")["severity"] == "LOW",
           "DU3b the summary is LOW, never a per-file MEDIUM")
        w(d, "metadata.json", '{"question": "Allocate the widget budget"}')
        v = gate_findings(d, P)
        trig = [x for x in v if x["kind"] == "data_trigger_column_unused"]
        ok(sorted(x["claim"].split("column `")[1].split("`")[0] for x in trig) == ["mix_a", "mix_b"]
           and all(x["severity"] == "MEDIUM" and x["mechanism_id"] == "mix_by_group" for x in trig),
           f"DU4 a required mechanism's trigger columns unread: MEDIUM each, linked to the mechanism, got {v}")
        ok("mix_a" not in str([x for x in v if x["kind"] == "data_columns_unread_summary"]),
           "DU4b trigger columns are not repeated in the summary")
        w(d, "models/mechanism_manifest.yaml", "mechanisms:\n- id: mix_by_group\n  status: simplified\n"
                                               "  assumption: one mix\n  sufficiency_test: models/s.yaml\n")
        ok(not any(x["kind"] == "data_trigger_column_unused" for x in gate_findings(d, P)),
           "DU5 a mechanism recorded as simplified (D1 checks its test) is not re-flagged here")
        os.remove(os.path.join(d, "models", "mechanism_manifest.yaml"))
        w(d, "data/processed/units.csv", "unit,pop\nA,1\n")
        w(d, "data/raw/src.csv", "unit,mix_raw\nA,1\n")
        ok([x["claim"].split("`")[1] for x in gate_findings(d, P) if x["kind"] == "data_trigger_column_unused"]
           == ["data/raw/src.csv"], "DU6 a trigger column only under data/raw/ is still flagged")
        w(d, "data/processed/units.csv",
          "unit,pop,rate_2020,rate_2021,mix_a,mix_b,lat,score_hist,state\nA,1,2,3,4,5,6,7,8\n")
        w(d, "models/reads_mix.py", "X = 'mix_a'\n")
        w(d, "data/raw/src.csv", "unit,mix_a,mix_b\nA,1,2\n")
        ok([x["claim"].split("column `")[1].split("`")[0] for x in gate_findings(d, P)
            if x["kind"] == "data_trigger_column_unused"] == ["mix_b"],
           "DU6b a raw-only trigger column is flagged even when another trigger column is processed and read")
        os.remove(os.path.join(d, "models", "reads_mix.py"))
        os.remove(os.path.join(d, "data", "raw", "src.csv"))
        w(d, "models/extra.py", 'COLS = "lat,state"\nx = df.mix_a\ndf.assign(mix_b=1)\nP = "score_" + "x"\n'
                                'T = f"{DP}/extra_table.csv"\nO = "other"\n')
        r = audit(d, P)
        ok(r["unread"] == {} and r["unopened"] == [] and r["triggers"] == [],
           f"DU7 code-like lists, attributes, keywords, prefixes and file names count, got {r}")
        os.remove(os.path.join(d, "models", "extra.py"))
        w(d, "models/data_use.yaml", "not_read_by_model:\n  - file: data/processed/extra_table.csv\n"
                                     "    columns: ['*']\n    reason: lookup table kept for the report maps\n"
                                     "  - file: data/processed/units.csv\n    columns: [mix_a, mix_b]\n"
                                     "    reason: mix is out of scope; see the scope declaration\n")
        v = gate_findings(d, P)
        ok(kinds(v) == ["data_columns_unread_summary"], f"DU8 exemptions with a reason clear findings, got {v}")
        w(d, "models/data_use.yaml", "not_read_by_model:\n  - file: data/processed/extra_table.csv\n"
                                     "    columns: ['*']\n    reason: ''\n")
        v = gate_findings(d, P)
        ok("data_use_invalid" in kinds(v) and "data_files_unused" in kinds(v),
           f"DU9 an exemption without a reason is invalid and exempts nothing, got {v}")
        w(d, "models/data_use.yaml", "not_read_by_model: [")
        ok("data_use_invalid" in kinds(gate_findings(d, P)), "DU10 unparseable data_use.yaml fails closed")
        os.remove(os.path.join(d, "models", "data_use.yaml"))
        w(d, "models/broken.py", "def f(:\n    return r['score_hist']\n")
        ok("score_hist" not in audit(d, P)["unread"].get("data/processed/units.csv", []),
           "DU11 an unparseable file is scanned as text")
        w(d, "data/processed/bad.csv", "")
        ok("data_use_unreadable" in kinds(gate_findings(d, P)), "DU12 an empty CSV fails closed")
    with tempfile.TemporaryDirectory() as d:
        w(d, "data/units.csv", "unit,pop,hist\nA,1,2\n")
        w(d, "models/m.py", "import pandas as pd\nU = pd.read_csv('data/units.csv')\nU['unit'], U['pop']\n")
        ok(audit(d)["unread"] == {"data/units.csv": ["hist"]},
           "DU13 without data/processed/, top-level data CSVs are the modelling data")
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
    v = gate_findings(a.run_dir)
    n_un = sum(len(x) for x in res["unread"].values())
    print(f"columns: {res['total']}  read: {res['read']}  exempt: {res['exempt']}  unread in opened files: {n_un}  "
          f"unopened files: {len(res['unopened'])}  mechanism columns unread: {len(res['triggers'])}")
    for x in v:
        print(f"[{x['severity']}] {x['kind']}: {x['claim'][:300]}")
    return 1 if any(x["severity"] in ("HIGH", "MEDIUM") for x in v) else 0


if __name__ == "__main__":
    sys.exit(main())
