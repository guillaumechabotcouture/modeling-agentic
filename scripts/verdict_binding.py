#!/usr/bin/env python3
"""A14: a pass verdict in the prose must be backed by the artifact it cites.

    python3 scripts/verdict_binding.py <run_dir>
    python3 scripts/verdict_binding.py --self-test

Numbers in the report are bound to computed values (render_claims, the claims
ledger, headline_binding, effect_concordance). Pass/fail VERDICTS were not: in
E7 attempt 8 round 4 a fix removed allocation options, the cited CSV's row went
empty (`usd_per_daly` blank, unit count 0), and results.md still said "The
cost-effectiveness clause holds (B17 in band; `models/cea_by_intervention.csv`)"
while its own benchmark table read "B17 ... | not computed this round | ... |
AGREE". Two critics caught it by hand.

What is read
------------
`results.md`, `decision_rule.md` and `report.md` (whichever exist), outside
fenced code. A statement is one prose sentence or one Markdown table row.

A statement is checked only when it carries a PASS verdict AND refers to a
computed artifact. Statements with a verdict but no artifact reference are not
this check's business; statements that only report a failure claim less than
the evidence and are left to the gates that own those checks (benchmark_match
blocks on drift).

  pass verdict     PASS / PASSES / PASSED / AGREE / AGREES (upper case, as a
                   verdict label), "holds", "passes", "passed", "in band",
                   "within (the) band", "is/are met". Not when negated
                   ("not in band", "no longer holds").
  artifact         a backticked run-relative data path (`*.csv`, `*.tsv`,
                   `*.yaml`, `*.yml`, `*.json`, optionally `path::dotted.key`)
                   in the same sentence, or, for a table row, in the row or in
                   the heading above the table; and/or a benchmark id from the
                   run's benchmark targets (models/benchmark_targets.yaml) or
                   from a cited benchmark file. In prose an id is bound to the
                   verdict when the verdict follows it within a few words, or
                   when it is the sentence's only id and the sentence states no
                   failure; in a table row, when it is the row's only id.

Requirements
------------
* Scope of a verdict. In a sentence, only the clauses (split at `;`, "but",
  ", while", outside brackets) that carry a pass verdict and state no failure;
  paths and row names in other clauses are not the verdict's. A table row that
  states both a pass and a failure is ambiguous and skipped.
* A bound benchmark id must not be out of band in the harness's own evaluation
  (scripts/benchmark_match.py `evaluate`); a target it cannot score is already
  benchmark_match's finding. In any cited benchmark file that lists the id
  (a `benchmarks:`/`results:` list, or an `id: value` mapping under `computed:`)
  the value, resolved as benchmark_match resolves it (`computed_value`, else
  `computed_field: <path>::<key>`), must be computed and inside the band.
* A table row whose verdict is a pass must not also say the value was "not
  computed" / "not evaluated".
* A cited CSV must have data rows. Columns are verdict columns (every filled
  cell a pass/fail word), count columns (numeric, `n_*` / `*_count`), metric
  columns (numeric) or text. The row(s) the statement is about (a text label
  with a word no other label has, appearing in the scope or in a bound
  benchmark's metric) must back it: a column the scope names (its header appears
  there) holds a value (a verdict column: not a failing word; a numeric column:
  not blank/NaN/non-finite; a count column: also not 0); with no column named,
  the row's single verdict column is not failing, or its metric cells are not
  all missing (a finite zero is a value). With no row identifiable, the file is
  flagged only when every row is missing, and never when it has verdict columns.
* A cited YAML/JSON must parse and not be empty; with `::key` the key must
  resolve to a non-empty, finite value that is not a failing verdict
  (FAIL, DRIFT, DISAGREE, ... or `false`).
* Containment: every file read (prose, targets, cited files, computed_field
  targets) must resolve, symlinks included, inside the run directory; one
  that does not is never opened (MEDIUM).

Findings
--------
HIGH `verdict_unbacked` when the artifact contradicts the verdict or holds no
computed value for what is cited; MEDIUM `verdict_artifact_unreadable` when the
cited file is missing, outside the run, or does not parse (fail closed: a
verdict whose evidence cannot be read is not accepted silently, but a missing
file is often a renamed path, which costs less to fix than a wrong verdict).
Neither is scope-declarable: the remedy (recompute, or reword the verdict) is
always available inside the pipeline.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from lib.safe_io import finite_number, inside_dir, safe_load_mapping  # noqa: E402
from lib.text_spans import code_spans, in_spans, resolve_dotpath  # noqa: E402

PROSE = (("results.md", "ANALYZE"), ("decision_rule.md", "ANALYZE"), ("report.md", "WRITE"))

_PASS_RE = re.compile(
    r"\b(?:PASS(?:ES|ED)?|AGREES?|[Hh]olds|[Pp]ass(?:es|ed)|(?:[Ww]ith)?in(?: the)? band|(?:is|are) met)\b")
_FAIL_RE = re.compile(
    r"\b(?:FAIL(?:S|ED)?|[Ff]ails|[Ff]ailed|DISAGREES?|DRIFT|out of band|outside (?:the )?band|not met|"
    r"does not hold|no longer holds)\b")
_NEG_RE = re.compile(r"(?:\bnot|\bno longer|n't|\bnever)\s+(?:\w+\s+)?$")
_PATH_RE = re.compile(r"`([\w./-]+\.(?:csv|tsv|ya?ml|json))(?:::([\w.\[\]*-]+))?`")
_NOT_COMPUTED_RE = re.compile(r"^\s*not (?:computed|evaluated)\b", re.IGNORECASE)
_HEADING_RE = re.compile(r"^#{1,6}\s")
_SEP_RE = re.compile(r"^\|[\s:|-]+\|$")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(*\[`])")
_FAILING = {"FAIL", "FAILED", "DRIFT", "MALFORMED", "DISAGREE", "NOT_MET", "REFUTED"}
_FAIL_CELLS = _FAILING | {"FAILS", "DISAGREES", "FALSE", "NO", "OUT_OF_BAND"}
_PASS_CELLS = {"PASS", "PASSED", "PASSES", "AGREE", "AGREES", "MET", "OK", "TRUE", "YES", "HOLDS",
               "IN_BAND", "WITHIN_BAND"}
_NAN_CELLS = {"nan", "inf", "-inf", "na", "n/a", "none", "null"}
_COUNT_RE = re.compile(r"^(?:n|num|count)(?:_|$)|_(?:n|count)$")
_CLAUSE_BREAK_RE = re.compile(r";|\s+but\s+|,\s+(?:while|whereas)\s+")
_ID_WINDOW = 40          # chars between a benchmark id and the verdict that follows it


def _snake(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower())


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9]{2,}", s.lower())}


def _passes(text: str) -> list[re.Match]:
    return [m for m in _PASS_RE.finditer(text) if not _NEG_RE.search(text[max(0, m.start() - 20):m.start()])]


# --------------------------------------------------------------------------- statements

def _statements(text: str):
    """Yield (line_no, statement, inherited_paths, is_table_row)."""
    fences = code_spans(text, inline=False)
    lines = text.split("\n")
    heading_paths: list[tuple[str, str]] = []
    pos = 0
    for i, line in enumerate(lines):
        start, pos = pos, pos + len(line) + 1
        if in_spans(fences, start):
            continue
        s = line.strip()
        if _HEADING_RE.match(s):
            heading_paths = _PATH_RE.findall(s)
            continue
        if s.startswith("|") and s.endswith("|"):
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if _SEP_RE.match(s) or _SEP_RE.match(nxt):
                continue          # separator or header row
            yield i + 1, s, heading_paths, True
            continue
        for sent in _SENT_SPLIT_RE.split(s):
            if sent:
                yield i + 1, sent, [], False


def _names(ids: set[str]) -> dict[str, str]:
    """{name as written in prose: id}: each id, plus its leading `_`-separated
    token when no other id shares it (prose writes `B1` for `B1_national_rate`)."""
    out = {i: i for i in ids}
    heads: dict[str, list[str]] = {}
    for i in ids:
        h = i.split("_")[0]
        if h != i and len(h) >= 2:
            heads.setdefault(h, []).append(i)
    out.update({h: v[0] for h, v in heads.items() if len(v) == 1 and h not in out})
    return out


def _bound_ids(stmt: str, ids: set[str], table: bool) -> list[str]:
    if not ids:
        return []
    found = sorted(((m.start(), m.end(), bid) for name, bid in _names(ids).items()
                    for m in re.finditer(rf"(?<![\w-]){re.escape(name)}(?![\w-])", stmt)))
    if not found:
        return []
    distinct = {b for _, _, b in found}
    if table:
        return list(distinct) if len(distinct) == 1 else []
    passes = _passes(stmt)
    if len(distinct) == 1 and not _FAIL_RE.search(stmt):
        return list(distinct)
    out = []
    for k, (s, e, bid) in enumerate(found):
        nxt = found[k + 1][0] if k + 1 < len(found) else len(stmt)
        if any(e <= m.start() <= e + _ID_WINDOW and m.start() < nxt for m in passes):
            out.append(bid)
    return sorted(set(out))


# --------------------------------------------------------------------------- artifacts

def _resolve_cited(run_dir: str, rel: str) -> tuple[str | None, str]:
    """(absolute path or None, why not). A path that resolves outside the run
    (`..`, absolute, or through a symlink) is never opened."""
    if os.path.isabs(rel) or ".." in rel.split("/"):
        return None, f"`{rel}` is outside the run directory"
    cands = [os.path.join(run_dir, rel)]
    if "/" not in rel:
        cands.append(os.path.join(run_dir, "models", rel))
    for c in cands:
        if os.path.lexists(c):
            if not inside_dir(run_dir, c):
                return None, f"`{rel}` resolves outside the run directory (symlink)"
            if os.path.isfile(c):
                return c, ""
    return None, f"`{rel}` does not exist in the run"


def _load_cited(path: str, rel: str, cache: dict):
    """(doc, error). CSV -> (header, rows); YAML/JSON -> parsed document."""
    if path in cache:
        return cache[path]
    try:
        if path.endswith((".csv", ".tsv")):
            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f, delimiter="\t" if path.endswith(".tsv") else ","))
            out = ((rows[0], rows[1:]) if rows else ([], [])), ""
        elif path.endswith(".json"):
            with open(path, encoding="utf-8") as f:
                out = json.load(f), ""
        else:
            doc, fs = safe_load_mapping(path, "verdict_artifact", display_path=rel)
            out = (doc, "") if not fs else (None, fs[0]["claim"])
    except (OSError, UnicodeDecodeError, ValueError, csv.Error) as e:
        out = None, f"`{rel}` cannot be read ({type(e).__name__}: {str(e)[:120]})"
    cache[path] = out
    return out


def _cell_value(cell: str):
    return finite_number(cell.strip().replace(",", "")) if cell is not None else None


def _vocab(cell: str) -> str:
    return re.sub(r"\s+", "_", cell.strip().upper())


def _col_kind(header: str, cells: list[str]) -> str:
    """'verdict' (every filled cell is a pass/fail word), 'count' / 'metric'
    (every filled cell is a number or a not-a-number token), else 'text'."""
    filled = [c.strip() for c in cells if c.strip()]
    if filled and all(_vocab(c) in _PASS_CELLS | _FAIL_CELLS for c in filled):
        return "verdict"
    if all(_cell_value(c) is not None or c.lower() in _NAN_CELLS for c in filled):
        return "count" if _COUNT_RE.search(_snake(header).strip("_")) else "metric"
    return "text"


def _row_problem(rel: str, name: str, header: list, r: list, kinds: list, named: list) -> str | None:
    """Why the row holds no computed value for what is cited, or None.
    Missing = blank / NaN / non-finite; a finite zero is a value, except in a
    count column the statement names (zero units: nothing was computed)."""
    counts = ", ".join(f"{header[j]}={r[j].strip() or 'blank'}" for j, k in enumerate(kinds) if k == "count")
    tail = f" ({counts})" if counts else ""
    for j in named:
        cell = r[j].strip()
        if kinds[j] == "verdict":
            if _vocab(cell) in _FAIL_CELLS:
                return f"`{rel}` row '{name}' records `{header[j]}` = {cell}"
            if not cell:
                return f"`{rel}` row '{name}' has no recorded `{header[j]}`"
        elif _cell_value(cell) is None:
            return f"`{rel}` row '{name}' has no computed `{header[j]}` (cell is {cell or 'blank'!r}){tail}"
        elif kinds[j] == "count" and _cell_value(cell) == 0:
            return f"`{rel}` row '{name}' has `{header[j]}` = 0: nothing was computed for it"
    if named:
        return None
    verdicts = [j for j, k in enumerate(kinds) if k == "verdict"]
    if len(verdicts) == 1 and r[verdicts[0]].strip():
        cell = r[verdicts[0]].strip()
        return f"`{rel}` row '{name}' records `{header[verdicts[0]]}` = {cell}" if _vocab(cell) in _FAIL_CELLS else None
    if verdicts:
        return None           # several verdict columns: which one is cited is ambiguous
    metrics = [j for j, k in enumerate(kinds) if k == "metric"]
    if metrics:
        if all(_cell_value(r[j]) is None for j in metrics):
            cells = ", ".join(f"{header[j]}={r[j].strip() or 'blank'}" for j in metrics)
            return f"`{rel}` row '{name}' holds no computed value ({cells}){tail}"
        return None
    cnt = [j for j, k in enumerate(kinds) if k == "count"]
    if cnt and all(not _cell_value(r[j]) for j in cnt):
        return f"`{rel}` row '{name}' holds no computed value{tail}"
    return None


def _csv_problems(rel: str, header: list, rows: list, context: str) -> list[str]:
    if not header or not rows:
        return [f"`{rel}` has no data rows"]
    width = len(header)
    rows = [r + [""] * (width - len(r)) for r in rows if any(c.strip() for c in r)]
    if not rows:
        return [f"`{rel}` has no data rows"]
    kinds = [_col_kind(header[j], [r[j] for r in rows]) for j in range(width)]
    label_col = next((j for j, k in enumerate(kinds) if k == "text"), None)
    nctx = _snake(context)
    named = [j for j, h in enumerate(header) if kinds[j] != "text" and len(_snake(h).strip("_")) >= 4
             and _snake(h).strip("_") in nctx]
    picked = []
    if label_col is not None:
        words = [_words(r[label_col]) for r in rows]
        ctx_words = _words(context)
        for k, r in enumerate(rows):
            others = set().union(*(w for m, w in enumerate(words) if m != k))
            full = _snake(r[label_col]).strip("_")
            if (words[k] - others) & ctx_words or (len(full) >= 4 and full in nctx):
                picked.append(r)
    if not picked:
        if "verdict" in kinds:
            return []         # which row's verdict is cited is ambiguous
        probs = [_row_problem(rel, "", header, r, kinds, named) for r in rows]
        return [f"`{rel}` holds no computed value in any row"] if all(probs) else []
    out = []
    for r in picked:
        p = _row_problem(rel, r[label_col], header, r, kinds, named)
        if p:
            out.append(p)
    return out


def _empty(v) -> bool:
    if v is None or (isinstance(v, (str, list, dict)) and not v):
        return True
    return isinstance(v, float) and not math.isfinite(v)


def _failing(v) -> bool:
    return v is False or (isinstance(v, str) and _vocab(v) in _FAIL_CELLS)


def _bench_entries(doc) -> dict[str, dict]:
    """{id: entry} of a benchmark file: a `benchmarks:`/`results:` list of
    entries with `id`, or a `computed:` mapping of id -> value."""
    out: dict[str, dict] = {}
    if not isinstance(doc, dict):
        return out
    for key in ("benchmarks", "results"):
        lst = doc.get(key)
        for e in lst if isinstance(lst, list) else []:
            if isinstance(e, dict) and e.get("id") is not None:
                out.setdefault(str(e["id"]), e)
    if isinstance(doc.get("computed"), dict):
        for k, v in doc["computed"].items():
            out.setdefault(str(k), {"id": str(k), "computed_value": v})
    return out


def _entry_problems(run_dir: str, rel: str, e: dict, bm, gate_r: dict | None) -> tuple[list[str], list[str]]:
    """(high, medium) for one benchmark entry of a cited file. The value is
    resolved as benchmark_match resolves it (`computed_value`, else
    `computed_field: <path>::<key>`, contained in the run), or `observed`."""
    bid = e.get("id")
    if e.get("within_band") is False:
        return [f"`{rel}` records {bid} outside its band (within_band: false)"], []
    if "computed_value" in e or "computed_field" in e:
        val, diag = bm._resolve_observed(run_dir, e)
        if val is None and "outside the run" in diag:
            return [], [f"`{rel}`: {bid} {diag}"]
    else:
        val, diag = e.get("observed"), ""
    v = finite_number(val)
    if v is None:
        return [f"`{rel}` has no computed value for {bid} ({diag or repr(val)})"], []
    lo, hi, _ = bm._band(e) if "target_value" in e else (
        (gate_r or {}).get("band_low"), (gate_r or {}).get("band_high"), "")
    if finite_number(lo) is not None and finite_number(hi) is not None and not lo <= v <= hi:
        return [f"`{rel}` has {bid} = {v:.4g}, outside its band [{lo:.4g}, {hi:.4g}]"], []
    return [], []


def _gate_problems(bid: str, r: dict) -> list[str]:
    """A pass verdict against the harness's own out-of-band value. A target the
    harness cannot score (no computed value, malformed) is benchmark_match's
    finding already, and the value often lives in a file the prose cites
    (targets are sha-locked once accepted), so it is not repeated here."""
    if r.get("malformed") or r.get("observed") is None:
        return []
    if not r.get("within_band"):
        lo, hi = (f"{v:.4g}" if isinstance(v, (int, float)) else "?" for v in (r.get("band_low"), r.get("band_high")))
        return [f"benchmark {bid} = {r['observed']:.4g} is outside its band [{lo}, {hi}] (scripts/benchmark_match.py)"]
    return []


# --------------------------------------------------------------------------- the check

def _clauses(stmt: str) -> list[str]:
    """Split a sentence at `;`, ` but `, `, while `, `, whereas ` outside
    brackets and backticks, so "(B1 in band; `x.csv`)" stays one clause."""
    out, depth, tick, start, i = [], 0, False, 0, 0
    while i < len(stmt):
        ch = stmt[i]
        if ch == "`":
            tick = not tick
        elif not tick and ch in "([":
            depth += 1
        elif not tick and ch in ")]":
            depth = max(0, depth - 1)
        elif not tick and depth == 0 and (m := _CLAUSE_BREAK_RE.match(stmt, i)):
            out.append(stmt[start:i])
            start = i = m.end()
            continue
        i += 1
    out.append(stmt[start:])
    return [c for c in out if c.strip()]


def _pass_scope(stmt: str, inherited: list, table: bool) -> tuple[str, list] | None:
    """(context, cited paths) the pass verdict is about, or None when the
    association is ambiguous. A table row: the row and its heading's paths,
    unless it also states a failure. A sentence: only the clauses that carry a
    pass verdict and no failure; paths in other clauses are not the verdict's."""
    if table:
        if _FAIL_RE.search(stmt):
            return None
        return stmt, list(dict.fromkeys(_PATH_RE.findall(stmt) + list(inherited)))
    clean = [c for c in _clauses(stmt) if _passes(c) and not _FAIL_RE.search(c)]
    if not clean:
        return None
    return " ".join(clean), list(dict.fromkeys(p for c in clean for p in _PATH_RE.findall(c)))


def _check_statement(run_dir, stmt, context, paths, table, gate, ids, cache, bm) -> tuple[list[str], list[str]]:
    """(high reasons, medium reasons) for one statement with a pass verdict."""
    high, med = [], []
    docs = []
    for rel, key in paths:
        p, why = _resolve_cited(run_dir, rel)
        if p is None:
            med.append(why)
            continue
        doc, err = _load_cited(p, rel, cache)
        if err:
            med.append(err)
            continue
        docs.append((rel, key, doc))
    cited_ids = {bid for _, _, d in docs for bid in _bench_entries(d)}
    bound = _bound_ids(stmt, ids | cited_ids, table)
    metrics = []
    for bid in bound:
        if bid in gate:
            high += _gate_problems(bid, gate[bid])
            metrics.append(str(gate[bid].get("metric") or ""))
    if table and bound and any(_NOT_COMPUTED_RE.match(c) for c in stmt.strip("|").split("|")):
        high.append("the row's own value cell says it was not computed")
    plain = re.sub(r"`[^`]*`", " ", context)
    for rel, key, doc in docs:
        listed = _bench_entries(doc)
        entries = [listed[bid] for bid in bound if bid in listed]
        for e in entries:
            h, m = _entry_problems(run_dir, rel, e, bm, gate.get(str(e.get("id"))))
            high += h
            med += m
            metrics.append(str(e.get("metric") or ""))
        if entries:
            continue
        if isinstance(doc, tuple):
            high += _csv_problems(rel, doc[0], doc[1], " ".join([plain, *metrics]))
        elif key:
            v = resolve_dotpath(doc, key)
            if _empty(v):
                high.append(f"`{rel}::{key}` holds no computed value ({v!r})")
            elif _failing(v):
                high.append(f"`{rel}::{key}` records {v!r}")
        elif _empty(doc):
            high.append(f"`{rel}` is empty")
    return high, med


def evaluate(run_dir: str) -> list[dict]:
    """One record per statement with a problem: file, line, statement, high, medium, stage."""
    bm = __import__("benchmark_match")
    out = []
    targets = os.path.join(run_dir, "models", "benchmark_targets.yaml")
    if os.path.lexists(targets) and not inside_dir(run_dir, targets):
        gate = {}
        out.append({"file": "models/benchmark_targets.yaml", "line": 0, "statement": "", "stage": "ANALYZE",
                    "high": [], "medium": ["models/benchmark_targets.yaml resolves outside the run directory "
                                           "(symlink), so benchmark verdicts cannot be checked"]})
    else:
        gate = {str(r.get("id")): r for r in bm.evaluate(run_dir).get("results") or []}
    ids = set(gate)
    cache: dict = {}
    for name, stage in PROSE:
        p = os.path.join(run_dir, name)
        if not os.path.lexists(p):
            continue
        if not inside_dir(run_dir, p):
            out.append({"file": name, "line": 0, "statement": "", "stage": stage, "high": [],
                        "medium": [f"{name} resolves outside the run directory (symlink); not read"]})
            continue
        try:
            with open(p, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as e:
            out.append({"file": name, "line": 0, "statement": "", "stage": stage, "high": [],
                        "medium": [f"{name} cannot be read ({type(e).__name__})"]})
            continue
        for line_no, stmt, inherited, table in _statements(text):
            if not _passes(stmt):
                continue
            scope = _pass_scope(stmt, inherited, table)
            if scope is None:
                continue
            context, paths = scope
            if not paths and not _bound_ids(stmt, ids, table):
                continue
            high, med = _check_statement(run_dir, stmt, context, paths, table, gate, ids, cache, bm)
            if high or med:
                out.append({"file": name, "line": line_no, "statement": stmt, "stage": stage,
                            "high": list(dict.fromkeys(high)), "medium": list(dict.fromkeys(med))})
    return out


def gate_findings(run_dir: str) -> list[dict]:
    """Findings for the STAGE 7 gate, HIGH if anything contradicts the verdict.
    Statements with the same problems (one benchmark restated in several
    places) are one finding listing every location, so one fix clears one item."""
    groups: dict[tuple, list[dict]] = {}
    for r in evaluate(run_dir):
        groups.setdefault((tuple(r["high"]), tuple(r["medium"])), []).append(r)
    out = []
    for (high, med), rs in groups.items():
        hi = bool(high)
        stage = "ANALYZE" if any(r["stage"] == "ANALYZE" for r in rs) else rs[0]["stage"]
        where = ", ".join(f"{r['file']}:{r['line']}" for r in rs[:6]) + (f" (+{len(rs) - 6} more)" if len(rs) > 6 else "")
        s = rs[0]["statement"]
        quote = s if len(s) <= 220 else s[:217] + "..."
        out.append({
            "kind": "verdict_unbacked" if hi else "verdict_artifact_unreadable",
            "severity": "HIGH" if hi else "MEDIUM",
            "stage": stage, "target_stage": stage, "scope_declarable": False,
            "claim": (f"{where} state(s) a pass verdict its cited evidence does not back: {'; '.join(high + med)}. "
                      f"Statement: \"{quote}\". Recompute the cited value, or reword the verdict to what the "
                      "artifact shows."),
        })
    return out


# --------------------------------------------------------------------------- self-test

_TARGETS = """benchmarks:
- id: B1
  metric: unit_cost_per_daly
  target_value: 40.0
  tolerance_factor: 5.0
  computed_value: 35.0
- id: B2
  metric: national_prevalence
  target_value: 0.2
  tolerance_abs: 0.03
  computed_value: 0.5
"""
_CSV = ("intervention,usd_per_daly,n_units\n"
        "Bednets campaign,47.3,309\n"
        "\"Chemoprevention (eligible)\",,0\n"
        "Spraying added to bednets,533.9,35\n")


def _self_test() -> int:
    fails: list[str] = []

    def ok(c, m):
        if not c:
            fails.append(m)

    def run(results: str, files: dict | None = None, targets: str | None = _TARGETS,
            outside: dict | None = None, links: dict | None = None):
        """`outside` files go in a directory beside the run; `links` maps a run
        path to a symlink target in that directory ('' = the directory itself)."""
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as ext:
            os.makedirs(os.path.join(d, "models"))
            for rel, body in (outside or {}).items():
                open(os.path.join(ext, rel), "w").write(body)
            for rel, tgt in (links or {}).items():
                os.symlink(os.path.join(ext, tgt) if tgt else ext, os.path.join(d, rel))
            if targets is not None and not os.path.lexists(os.path.join(d, "models", "benchmark_targets.yaml")):
                open(os.path.join(d, "models", "benchmark_targets.yaml"), "w").write(targets)
            for rel, body in (files or {}).items():
                open(os.path.join(d, rel), "w").write(body)
            if not os.path.lexists(os.path.join(d, "results.md")):
                open(os.path.join(d, "results.md"), "w").write(results)
            return gate_findings(d)

    base = {"models/cea.csv": _CSV}
    # V1: the attempt-8 shape: verdict + benchmark id + CSV whose cited row is empty.
    bc = "benchmarks:\n- id: B1\n  metric: chemoprevention_usd_per_daly\n  target_value: 40.0\n" \
         "  tolerance_factor: 5.0\n  computed_value: .nan\n"
    f = run("# R\n\nThe cost clause holds (B1 in band; `models/cea.csv`).\n",
            {**base, "models/bench_computed.yaml": bc},
            targets=_TARGETS.replace("unit_cost_per_daly", "chemoprevention_usd_per_daly"))
    ok(len(f) == 1 and f[0]["severity"] == "HIGH" and f[0]["kind"] == "verdict_unbacked"
       and "Chemoprevention" in f[0]["claim"] and "usd_per_daly" in f[0]["claim"]
       and "results.md:3" in f[0]["claim"] and f[0]["scope_declarable"] is False, f"V1 empty cited row: {f}")
    # V1b: the table row of the same run: AGREE next to "not computed", cited file NaN.
    f = run("## Benchmarks (`models/bench_computed.yaml`)\n| ID | Ours | Verdict |\n|---|---|---|\n"
            "| B1 cost | not computed this round | AGREE |\n| B2 prev | 0.5 | DISAGREE |\n",
            {"models/bench_computed.yaml": bc})
    ok(len(f) == 1 and f[0]["severity"] == "HIGH" and "not computed" in f[0]["claim"]
       and "no computed value for B1" in f[0]["claim"], f"V1b table row: {f}")
    # V2: contradicted: a pass verdict for a benchmark the harness finds out of band;
    # restated in a table, it is still one finding listing both places.
    f = run("Prevalence is in band (B2).\n\n| ID | Verdict |\n|---|---|\n| B2 prev | AGREE |\n")
    ok(len(f) == 1 and f[0]["severity"] == "HIGH" and "outside its band" in f[0]["claim"]
       and "results.md:1, results.md:5" in f[0]["claim"], f"V2 contradicted: {f}")
    # V2b: a ::key that records a failing verdict.
    f = run("The robustness check passes (`models/rob.yaml::verdict`).\n", {"models/rob.yaml": "verdict: FAIL\n"})
    ok(len(f) == 1 and f[0]["severity"] == "HIGH" and "'FAIL'" in f[0]["claim"], f"V2b failing key: {f}")
    # V3: correct verdicts, no finding (row with values; benchmark in band; key resolves).
    f = run("The bednet clause holds (`models/cea.csv`, bednets campaign).\n\n"
            "Cost per DALY is in band (B1).\n\n| ID | Verdict |\n|---|---|\n| B1 cost | AGREE |\n\n"
            "The check passes (`models/rob.yaml::summary.n`).\n",
            {**base, "models/rob.yaml": "summary:\n  n: 12\n"})
    ok(f == [], f"V3 correct verdicts: {f}")
    # V4: missing cited file fails closed (MEDIUM, not silence); outside the run too.
    f = run("The coverage clause holds (`models/gone.csv`).\n\nIt passes (`../x.yaml`).\n")
    ok(len(f) == 2 and all(x["severity"] == "MEDIUM" and x["kind"] == "verdict_artifact_unreadable" for x in f)
       and "does not exist" in f[0]["claim"] and "outside the run" in f[1]["claim"], f"V4 missing file: {f}")
    # V4b: unparseable YAML is MEDIUM.
    f = run("The check passes (`models/bad.yaml`).\n", {"models/bad.yaml": "a: [1,\n"})
    ok(len(f) == 1 and f[0]["severity"] == "MEDIUM", f"V4b unparseable: {f}")
    # V5: a verdict with no artifact reference is ignored; so is a failure, a negation and fenced code.
    f = run("The coverage clause holds.\n\nThe clause fails (`models/cea.csv`, chemoprevention).\n\n"
            "B2 is not in band.\n\n```\nholds (`models/gone.csv`)\n```\n", base)
    ok(f == [], f"V5 ignored statements: {f}")
    # V6: no identifiable row: a file with values passes, an all-empty file does not.
    f = run("The cost clause holds (`models/cea.csv`).\n", base)
    ok(f == [], f"V6a unidentified row with values elsewhere: {f}")
    f = run("The cost clause holds (`models/z.csv`).\n", {"models/z.csv": "unit,value\na,\nb,nan\n"})
    ok(len(f) == 1 and f[0]["severity"] == "HIGH" and "no computed value in any row" in f[0]["claim"],
       f"V6b all-empty: {f}")
    # V7: with several ids, only the one the verdict follows is bound (B2 stated out of band).
    f = run("Cost is B1 in band; prevalence B2 out of band.\n")
    ok(f == [], f"V7 id binding: {f}")
    # V7b: prose writes the id's leading token (`B2` for `B2_prevalence`) when it is unique.
    f = run("| ID | Verdict |\n|---|---|\n| B2 | AGREE |\n", targets=_TARGETS.replace("id: B2", "id: B2_prevalence"))
    ok(len(f) == 1 and "B2_prevalence" in f[0]["claim"], f"V7b id alias: {f}")
    # V8: no benchmark targets at all and no paths: nothing to bind.
    f = run("All benchmarks AGREE (B1).\n", targets=None)
    ok(f == [], f"V8 no targets: {f}")
    # V9: a target the harness cannot score is benchmark_match's finding, not repeated here;
    # a cited `computed:` mapping is read with the harness's band (in band: fine; out: HIGH).
    unscored = _TARGETS.replace("  computed_value: 0.5\n", "")
    f = run("| ID | Verdict |\n|---|---|\n| B2 | AGREE |\n", targets=unscored)
    ok(f == [], f"V9a unscored target: {f}")
    f = run("## Benchmarks (`models/bc.yaml`)\n| ID | Verdict |\n|---|---|\n| B2 | AGREE |\n| B1 | AGREE |\n",
            {"models/bc.yaml": "computed:\n  B2: 0.21\n  B1: 900.0\n"}, targets=unscored)
    ok(len(f) == 1 and "B1 = 900" in f[0]["claim"] and "results.md:5" in f[0]["claim"], f"V9b computed mapping: {f}")

    # Codex round 1 on #63 (C1-C5): each was a false HIGH or an unchecked read.
    # C1: a cited file, a cited file's parent dir, a prose file or the targets file that is a
    # symlink out of the run is never read: MEDIUM, and nothing from outside is checked.
    f = run("The cost clause holds (`models/ext.csv`).\n", outside={"x.csv": "unit,value\na,\n"},
            links={"models/ext.csv": "x.csv"})
    ok(len(f) == 1 and f[0]["severity"] == "MEDIUM" and "symlink" in f[0]["claim"], f"C1a symlinked file: {f}")
    f = run("The cost clause holds (`ext/x.csv`).\n", outside={"x.csv": "unit,value\na,\n"}, links={"ext": ""})
    ok(len(f) == 1 and f[0]["severity"] == "MEDIUM" and "symlink" in f[0]["claim"], f"C1b symlinked dir: {f}")
    f = run("", outside={"r.md": "The cost clause holds (`models/cea.csv`, chemoprevention).\n"},
            links={"results.md": "r.md"}, files=base)
    ok(len(f) == 1 and f[0]["severity"] == "MEDIUM" and "results.md resolves outside" in f[0]["claim"],
       f"C1c symlinked prose file: {f}")
    f = run("Prevalence is in band (B2).\n", outside={"t.yaml": _TARGETS},
            links={"models/benchmark_targets.yaml": "t.yaml"})
    ok(len(f) == 1 and f[0]["severity"] == "MEDIUM" and "benchmark_targets.yaml resolves outside" in f[0]["claim"],
       f"C1d symlinked targets: {f}")
    # C2: a cited benchmark entry with `computed_field:` is resolved through benchmark_match
    # (in band: no finding), and a computed_field out of the run is MEDIUM, not read.
    cf = ("benchmarks:\n- id: B1\n  metric: m\n  target_value: 40.0\n  tolerance_factor: 5.0\n"
          "  computed_field: models/out.yaml::cost.v\n")
    f = run("## B (`models/cf.yaml`)\n| ID | Verdict |\n|---|---|\n| B1 | AGREE |\n",
            {"models/cf.yaml": cf, "models/out.yaml": "cost:\n  v: 35.0\n"})
    ok(f == [], f"C2a computed_field resolved: {f}")
    f = run("## B (`models/cf.yaml`)\n| ID | Verdict |\n|---|---|\n| B1 | AGREE |\n",
            {"models/cf.yaml": cf.replace("models/out.yaml", "ext/out.yaml")},
            outside={"out.yaml": "cost:\n  v: 35.0\n"}, links={"ext": ""})
    ok(len(f) == 1 and f[0]["severity"] == "MEDIUM" and "outside the run" in f[0]["claim"],
       f"C2b computed_field outside the run: {f}")
    # C3: a finite zero is a computed value; zero is missing only in a count column the
    # statement names. The attempt-8 shape (blank metric, count 0) still fails (V1).
    zero = "check,bias,n_units\nalpha,0.0,12\nbeta,1.5,3\n"
    f = run("The alpha bias check passes (`models/z.csv`).\n", {"models/z.csv": zero})
    ok(f == [], f"C3a zero bias is a value: {f}")
    f = run("The alpha check passes (`models/z.csv`).\n", {"models/z.csv": zero})
    ok(f == [], f"C3b zero in an unnamed row: {f}")
    f = run("The alpha check passes on n_units (`models/z.csv`).\n", {"models/z.csv": zero.replace(",12", ",0")})
    ok(len(f) == 1 and "n_units` = 0" in f[0]["claim"], f"C3c named zero count: {f}")
    # C4: a failure in another clause of the same sentence does not leak into the pass clause.
    two = "intervention,cost\nAlpha,12.0\nBeta,\n"
    f = run("Alpha passes (`models/two.csv`); Beta fails because its cost was not computed.\n",
            {"models/two.csv": two})
    ok(f == [], f"C4a other clause ignored: {f}")
    f = run("Alpha passes (`models/two.csv`); Beta's cost was not computed.\n", {"models/two.csv": two})
    ok(f == [], f"C4b row named only in a clause without the verdict: {f}")
    f = run("Alpha passes but Beta fails (`models/two.csv`).\n", {"models/two.csv": two})
    ok(f == [], f"C4c path in the failing clause: skipped as ambiguous: {f}")
    f = run("Beta passes (`models/two.csv`); Alpha is fine.\n", {"models/two.csv": two})
    ok(len(f) == 1 and "'Beta'" in f[0]["claim"], f"C4d the pass clause is still checked: {f}")
    # C5: a categorical verdict column is read as a verdict, not as a missing number.
    vc = "check,verdict\nalpha,PASS\nbeta,FAIL\n"
    f = run("The alpha check passes (`models/v.csv`).\n", {"models/v.csv": vc})
    ok(f == [], f"C5a verdict PASS supports: {f}")
    f = run("The beta check passes (`models/v.csv`).\n", {"models/v.csv": vc})
    ok(len(f) == 1 and f[0]["severity"] == "HIGH" and "= FAIL" in f[0]["claim"], f"C5b verdict FAIL contradicts: {f}")
    f = run("The check passes (`models/v.csv`).\n", {"models/v.csv": vc})
    ok(f == [], f"C5c no row identified, verdict column: ambiguous, skipped: {f}")

    if fails:
        print(f"FAIL: {len(fails)} case(s)", file=sys.stderr)
        for m in fails:
            print(f"  - {m}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()
    if len(sys.argv) != 2 or not os.path.isdir(sys.argv[1]):
        print(__doc__)
        return 2
    fs = gate_findings(sys.argv[1])
    for x in fs:
        print(f"[{x['severity']}] {x['kind']}: {x['claim']}")
    print(f"{len(fs)} finding(s)")
    return 1 if any(x["severity"] == "HIGH" for x in fs) else 0


if __name__ == "__main__":
    sys.exit(main())
