#!/usr/bin/env python3
"""Phase 34: a test for every finding, kept as a regression suite.

E7 attempt 6 did not converge: six moderate findings took 4-5 attempts each
("narrowed, still present") because what counted as fixed moved between
rounds, and the round-6 fix broke three things nobody re-checked. Here each
HIGH / MEDIUM critic finding carries a `done_when` test the harness runs
against the run's files, without a model:

    done_when:
      - {file: model_output.json, key: total_cost_usd.ci95, op: width_gt, value: 0}
      - {file: summary.md, contains: "re-anchored range \\d+"}

Forms (a list means all must pass):
    {file, exists: true}                     the file exists
    {file, contains: <regex>}                regex found (case-insensitive)
    {file, lacks: <regex>}                   regex not found
    {file, key: a.b.0, op, value}            a JSON / YAML value; op is one of
        == != < <= > >= between [lo, hi]  in [..]  matches <regex>
        width_gt (a [lo, hi] pair, hi - lo > value)
    {file: x.csv, column, op, value, rows: all | any | none}   every / some / no row passes
    {file: x.csv, column, op, value, min_rows: N}               at least N rows pass
    {file: x.csv, column, agg: sum | mean | min | max | count, op, value}
        (any CSV form may add where: {column: value} to test only the matching rows)
    {judgement: "<what would show it is fixed>"}   no runnable test fits; in a list
        with runnable checks, those must pass and a critic decides the rest

α  The test is frozen when the gate first sees it valid (a critic cannot move
   the target; one who thinks a test too weak raises a new finding with a new
   id). A finding whose test passes is resolved at the gate, whatever the
   critic's `resolved`. A HIGH / MEDIUM finding with no valid test still
   blocks, and adds one MEDIUM per critic naming the ids.
β  Every test that has passed is re-run at every gate (done_when_registry.json,
   written only by the gate). One that fails again reopens its finding as a
   HIGH `done_when_regressed`, naming the round it passed. A finding the
   critic resolves while its runnable test fails is `done_when_unmet` (its own
   severity) until the test passes or the critic withdraws it
   (`done_when_withdrawn: <reason>`). Critics need no fix check for items with
   a runnable test: the harness checks them.

Agents check their own work before returning:

    python3 scripts/done_when.py check <run_dir> [ID ...]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile

import yaml

REGISTRY = "done_when_registry.json"
CRITICS = ("critique-methods", "critique-domain", "critique-presentation", "critique-redteam")
OPS = ("==", "!=", "<", "<=", ">", ">=", "between", "in", "width_gt", "matches")
_MAX_BYTES = 5_000_000

CRITIC_NOTE = """

## Give every finding a test (Phase 34)

Every HIGH or MEDIUM blocker needs a `done_when:` field: a test the harness
runs on the run's files to decide when the finding is fixed. Write the
smallest test that would only pass if the problem were really fixed:

```yaml
done_when:
  - {file: models/uq_summary.json, key: total_cost_usd.ci95, op: width_gt, value: 0}
  - {file: results.md, contains: "cases averted[^\\n]*\\d+[–-]\\d+"}
```

Forms (a list means all must pass): `{file, exists: true}`;
`{file, contains: <regex>}` / `{file, lacks: <regex>}` (case-insensitive);
`{file, key: a.b.0, op, value}` on a JSON or YAML file, with op one of
`== != < <= > >= between in matches width_gt`; `{file: x.csv, column, op, value,
rows: all | any | none}` or `min_rows: N`, optionally `where: {column: value}`,
or `agg: sum | mean | min | max | count`, for a CSV column (e.g. no row with
package IRS: `{file: allocation.csv, column: package, op: matches, value: IRS,
rows: none}`; total cost within the cap: `{file: allocation.csv, column: cost,
agg: sum, op: "<=", value: 100}`).
Paths are relative to the run
directory. Prefer numbers and structure in output files over wording in
prose, which changes: a fix that says the same thing in other words must
pass. For text, match the numbers or a short label, never a phrase:

```yaml
# good: the interval's numbers appear in the Summary
- {file: summary.md, contains: "1\\.8\\D{1,4}2\\.6"}
# good: a value in an output file
- {file: models/uq_summary.json, key: p_exceed_cap, op: ">", value: 0}
# bad: passes only for one way of writing it ("cases averted" vs "averts ... cases")
- {file: summary.md, contains: "cases averted[^\\n]*\\d+-\\d+"}
``` When no runnable test fits, write
`done_when: {judgement: "<what would show it is fixed>"}`; when runnable checks
are necessary but not enough, list them with one judgement entry.

The test is fixed once the gate has seen it: a finding whose test passes is
resolved, and tests that passed are re-run every round, so a later change
that breaks one reopens it. Do not keep a finding open with a new
requirement; raise a new finding (new id, its own test). A finding you mark
resolved while its test still fails stays blocking; if the test itself was
wrong, add `done_when_withdrawn: "<why>"` to the finding. Run
`python3 scripts/done_when.py check <run_dir>` to see which tests pass now.
"""

WORKER_NOTE = """

## Check your fixes against their tests (Phase 34)

Findings you are asked to fix carry a `done_when` test (shown in
open_problems.md). Before you return, run
`python3 scripts/done_when.py check <run_dir> <ID> ...` for the ids in your
task, and keep working until they pass or you can say why they cannot.
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _as_list(dw) -> list | None:
    if isinstance(dw, dict):
        return [dw]
    if isinstance(dw, list) and dw and all(isinstance(c, dict) for c in dw):
        return dw
    return None


def _safe_rel(path) -> str | None:
    p = str(path or "").strip()
    if not p or os.path.isabs(p) or p.startswith("~"):
        return None
    norm = os.path.normpath(p)
    if norm.startswith("..") or norm.split(os.sep)[0] in ("_truth", ".env") or ".env" in os.path.basename(norm):
        return None
    return norm


def _in_run(rel: str, run_dir: str) -> str:
    """Critics often write paths as the lead sees them, from the repo root
    (`runs/<run>/models/x.csv`; E7 attempt 6 cited code that way): strip the
    run directory's own prefix so the test reads the run's file."""
    name = os.path.basename(os.path.normpath(run_dir))
    for prefix in (os.path.join("runs", name) + os.sep, name + os.sep):
        if name and rel.startswith(prefix):
            return rel[len(prefix):]
    return rel


def problems(dw) -> list[str]:
    """Why `dw` is not a valid done_when (empty list = valid)."""
    checks = _as_list(dw)
    if checks is None:
        return ["done_when must be a mapping or a non-empty list of mappings"]
    out = []
    for c in checks:
        if "judgement" in c:
            if not str(c.get("judgement") or "").strip() or sum("judgement" in x for x in checks) > 1:
                out.append("give one judgement entry that says what would show the fix")
            continue
        if _safe_rel(c.get("file")) is None:
            out.append(f"file must be a path inside the run directory: {c.get('file')!r}")
            continue
        forms = [k for k in ("exists", "contains", "lacks", "key", "column") if k in c]
        if len(forms) != 1:
            out.append(f"{c.get('file')}: give exactly one of exists / contains / lacks / key / column")
            continue
        if forms[0] == "column" and c.get("rows", "all") not in ("all", "any", "none"):
            out.append(f"{c.get('file')}: rows must be all, any or none")
        if forms[0] == "column" and c.get("agg") not in (None, "sum", "mean", "min", "max", "count"):
            out.append(f"{c.get('file')}: agg must be sum, mean, min, max or count")
        if forms[0] == "column" and not isinstance(c.get("where") or {}, dict):
            out.append(f"{c.get('file')}: where must map column names to values")
        if forms[0] == "column" and "min_rows" in c and not (
                isinstance(c["min_rows"], int) and not isinstance(c["min_rows"], bool) and c["min_rows"] > 0):
            out.append(f"{c.get('file')}: min_rows must be a positive whole number")
        if forms[0] in ("contains", "lacks"):
            if not isinstance(c[forms[0]], str) or not c[forms[0]].strip():
                out.append(f"{c.get('file')}: {forms[0]} needs a non-empty regex")   # "" matches everything
                continue
            try:
                re.compile(str(c[forms[0]]))
            except re.error as e:
                out.append(f"{c.get('file')}: bad regex: {e}")
        if forms[0] == "exists" and not isinstance(c["exists"], bool):
            out.append(f"{c.get('file')}: exists must be true or false")
        if forms[0] in ("key", "column") and not str(c.get(forms[0]) or "").strip():
            out.append(f"{c.get('file')}: {forms[0]} must name a {'value' if forms[0] == 'key' else 'column'}")
        if forms[0] in ("key", "column") and "value" not in c:
            out.append(f"{c.get('file')}: give the value to compare with")
        if forms[0] in ("key", "column") and c.get("op") in ("<", "<=", ">", ">=", "width_gt") \
                and not _is_number(c.get("value")):
            out.append(f"{c.get('file')}: {c.get('op')} needs a number as value")
        if forms[0] in ("key", "column") and c.get("op") == "between" and isinstance(c.get("value"), list) \
                and not all(_is_number(x) for x in c["value"]):
            out.append(f"{c.get('file')}: between needs two numbers")
        if forms[0] in ("key", "column"):
            op = c.get("op")
            if op not in OPS:
                out.append(f"{c.get('file')}: op must be one of {', '.join(OPS)}")
            elif op == "between" and not (isinstance(c.get("value"), list) and len(c["value"]) == 2):
                out.append(f"{c.get('file')}: between needs value [lo, hi]")
            elif op == "in" and not isinstance(c.get("value"), list):
                out.append(f"{c.get('file')}: in needs a list value")
            elif op == "matches":
                try:
                    re.compile(str(c.get("value")))
                except re.error as e:
                    out.append(f"{c.get('file')}: bad regex: {e}")
    return out


def mechanical(dw) -> bool:
    checks = _as_list(dw)
    return bool(checks) and not problems(dw) and not any("judgement" in c for c in checks)


def _read(run_dir: str, rel: str):
    path = os.path.join(run_dir, rel)
    if os.path.getsize(path) > _MAX_BYTES:
        raise ValueError(f"{rel} is larger than {_MAX_BYTES // 1_000_000} MB")
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _lookup(doc, key: str):
    cur = doc
    for part in str(key).split("."):
        if isinstance(cur, list) and re.fullmatch(r"-?\d+", part):
            cur = cur[int(part)]
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise KeyError(f"no {part!r} in {key}")
    return cur


def _is_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _cell(v: str):
    """A CSV cell as a number when it reads as one, else the stripped text."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return str(v or "").strip()


def _num(x):
    if isinstance(x, bool):
        raise TypeError("boolean, not a number")
    return float(x)


def _compare(v, op: str, want) -> bool:
    if op == "==":
        if v == want:
            return True
        try:   # 1 == 1.0 and "1" (a CSV cell) == 1; anything else is simply unequal
            return _num(v) == _num(want)
        except (TypeError, ValueError):
            return False
    if op == "!=":
        return not _compare(v, "==", want)
    if op == "in":
        return v in want
    if op == "matches":   # regex search on the text of the value, case-insensitive
        return re.search(str(want), str(v), re.I) is not None
    if op == "between":
        return _num(want[0]) <= _num(v) <= _num(want[1])
    if op == "width_gt":
        if not (isinstance(v, list) and len(v) == 2):
            raise TypeError("width_gt needs a [lo, hi] pair")
        return _num(v[1]) - _num(v[0]) > _num(want)
    a, b = _num(v), _num(want)
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


def evaluate(dw, run_dir: str) -> tuple[bool | None, str]:
    """(True / False, detail) for a runnable test; (None, why) for a
    judgement or an invalid test. Never raises."""
    bad = problems(dw)
    if bad:
        return None, "invalid: " + "; ".join(bad)
    checks = _as_list(dw)
    judged = [c for c in checks if "judgement" in c]
    details = []
    for c in [c for c in checks if "judgement" not in c]:
        rel = _safe_rel(_in_run(_safe_rel(c["file"]), run_dir))   # re-checked after the prefix is stripped
        if rel is None:
            return None, f"invalid: {c['file']!r} is not a run file"
        try:
            if "exists" in c:
                ok = os.path.exists(os.path.join(run_dir, rel)) == bool(c["exists"])
                d = f"{rel} {'exists' if os.path.exists(os.path.join(run_dir, rel)) else 'missing'}"
            elif not os.path.exists(os.path.join(run_dir, rel)):
                ok, d = False, f"{rel} missing"
            elif "contains" in c or "lacks" in c:
                pat = str(c.get("contains", c.get("lacks")))
                hit = re.search(pat, _read(run_dir, rel), re.I | re.M)
                ok = bool(hit) == ("contains" in c)
                d = (f"{rel}: found {hit.group(0)[:80]!r}" if hit else f"{rel}: no match for {pat[:60]!r}")
            elif "column" in c:
                import csv
                import io
                rows = list(csv.DictReader(io.StringIO(_read(run_dir, rel))))
                if not rows or c["column"] not in rows[0]:
                    raise KeyError(f"no column {c['column']!r}")
                for wk, wv in (c.get("where") or {}).items():   # keep rows whose column equals the value
                    if wk not in rows[0]:
                        raise KeyError(f"no column {wk!r}")
                    rows = [r for r in rows if _compare(_cell(r[wk]), "==", wv)]
                if not rows:   # a filter that keeps nothing tests nothing
                    raise ValueError(f"no rows match where {c.get('where')}")
                if c.get("agg"):
                    if c["agg"] == "count":
                        agg = len(rows)
                    else:
                        vals = [_cell(r[c["column"]]) for r in rows]
                        if any(v == "" for v in vals):   # a blank cell is missing output, not zero
                            raise ValueError(f"{sum(v == '' for v in vals)} blank {c['column']!r} value(s)")
                        nums = [_num(v) for v in vals]
                        agg = {"sum": sum(nums), "mean": sum(nums) / len(nums),
                               "min": min(nums), "max": max(nums)}[c["agg"]]
                    ok = _compare(agg, c["op"], c.get("value"))
                    d = f"{rel}: {c['agg']}({c['column']}) = {agg:g} ({c['op']} {c.get('value')})"
                    details.append(("pass " if ok else "FAIL ") + d)
                    if not ok:
                        return False, "; ".join(details)
                    continue
                hits = [_compare(_cell(r[c["column"]]), c["op"], c.get("value")) for r in rows]
                mode = c.get("rows", "all")
                if "min_rows" in c:
                    ok, mode = sum(hits) >= int(c["min_rows"]), f"at least {int(c['min_rows'])}"
                else:
                    ok = all(hits) if mode == "all" else any(hits) if mode == "any" else not any(hits)
                d = (f"{rel}: {sum(hits)} of {len(rows)} rows have {c['column']} {c['op']} {c.get('value')} "
                     f"(need {mode})")
            else:
                text = _read(run_dir, rel)
                doc = json.loads(text) if rel.endswith(".json") else yaml.safe_load(text)
                v = _lookup(doc, c["key"])
                ok = _compare(v, c["op"], c.get("value"))
                d = f"{rel}: {c['key']} = {json.dumps(v, default=str)[:80]} ({c['op']} {c.get('value')})"
        except (OSError, ValueError, KeyError, IndexError, TypeError, yaml.YAMLError) as e:
            ok, d = False, f"{rel}: {type(e).__name__}: {str(e)[:120]}"
        details.append(("pass " if ok else "FAIL ") + d)
        if not ok:
            return False, "; ".join(details)
    if judged:   # the runnable parts are necessary; a critic decides the rest
        return None, "; ".join(details + ["judgement: " + str(judged[0]["judgement"])[:200]])
    return True, "; ".join(details)


# ---------------------------------------------------------------------------
# α/β: registry, frozen tests, regressions (gate side)
# ---------------------------------------------------------------------------

def load_registry(run_dir: str) -> dict:
    try:
        with open(os.path.join(run_dir, REGISTRY), encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) and isinstance(doc.get("tests"), dict) else {"tests": {}}
    except (OSError, ValueError):
        return {"tests": {}}


def _blockers(critiques: dict):
    for reviewer, doc in (critiques or {}).items():
        if reviewer not in CRITICS:
            continue
        for b in (doc or {}).get("blockers") or []:
            if isinstance(b, dict) and b.get("severity") in ("HIGH", "MEDIUM") and b.get("id"):
                yield reviewer, b


def run(run_dir: str, critiques: dict, round_n: int, record: bool = True, exclude=()) -> dict:
    """Evaluate every test (frozen ones from the registry, new valid ones from
    the critiques) and, when `record`, save the registry. `exclude`: ids the
    gate does not count (outside the decision brief). Returns
    {resolved: [ids whose test passes while the critic has them open],
     regressed: [tests that passed at an earlier gate run and fail now],
     unmet: [findings a critic resolved whose runnable test fails and never passed],
     missing: {reviewer: [ids with no valid test]}, invalid: [registry ids that cannot be read],
     status: {id: {passes, detail, mechanical, frozen_round}}}."""
    reg = load_registry(run_dir)
    tests = reg["tests"]
    exclude = {str(x) for x in exclude or ()}
    missing: dict[str, list] = {}
    open_ids, closed_ids = set(), set()
    for reviewer, b in _blockers(critiques):
        bid = str(b["id"])
        if bid in exclude:
            continue
        (closed_ids if b.get("resolved") is True else open_ids).add(bid)
        if bid in tests:
            t = tests[bid]
            # A critic may withdraw a frozen test, with its reason: a visible,
            # counted change of target, never a silent one.
            why = str(b.get("done_when_withdrawn") or "").strip()
            if why and isinstance(t, dict) and not t.get("withdrawn"):
                t["withdrawn"] = {"round": round_n, "reason": why[:300]}
            continue  # frozen: later edits to the critic's test are ignored
        dw = b.get("done_when")
        if dw is None or problems(dw):
            if b.get("resolved") is not True:
                missing.setdefault(reviewer, []).append(bid)
            continue
        tests[bid] = {"reviewer": reviewer, "severity": b["severity"], "claim": str(b.get("claim") or "")[:200],
                      "target_stage": str(b.get("target_stage") or "").upper(), "done_when": dw,
                      "frozen_round": round_n, "passed_round": None, "history": []}
        why = str(b.get("done_when_withdrawn") or "").strip()
        if why:
            tests[bid]["withdrawn"] = {"round": round_n, "reason": why[:300]}
    resolved, regressed, unmet, invalid, status = [], [], [], [], {}
    for bid, t in tests.items():
        if bid in exclude:
            continue
        if not isinstance(t, dict) or "done_when" not in t or not isinstance(t.get("history", []), list):
            invalid.append(bid)   # fail closed: a frozen test that cannot be read is a finding
            continue
        if t.get("withdrawn"):
            status[bid] = {"passes": None, "detail": f"withdrawn in round {t['withdrawn'].get('round')}: "
                                                     f"{t['withdrawn'].get('reason')}", "mechanical": False,
                           "frozen_round": t.get("frozen_round")}
            continue
        passes, detail = evaluate(t["done_when"], run_dir)
        status[bid] = {"passes": passes, "detail": detail, "mechanical": mechanical(t["done_when"]),
                       "frozen_round": t.get("frozen_round")}
        entry = {"id": bid, "severity": t.get("severity") or "HIGH", "reviewer": t.get("reviewer"),
                 "passed_round": t.get("passed_round"), "detail": detail, "claim": t.get("claim") or "",
                 "target_stage": t.get("target_stage") or "MODEL"}
        if passes is True:
            if bid in open_ids:
                resolved.append(bid)
            if t.get("passed_round") is None:
                t["passed_round"] = round_n
        elif passes is False and t.get("passed_round") is not None:
            regressed.append(entry)   # any earlier gate run, the same round included
        elif passes is False and bid in closed_ids:
            unmet.append(entry)
        t["history"] = [h for h in t.get("history") or [] if h[0] != round_n] + [[round_n, passes]]
    if record:
        _save(run_dir, reg)
    return {"resolved": sorted(resolved), "regressed": regressed, "unmet": unmet, "missing": missing,
            "invalid": sorted(invalid), "status": status}


def _save(run_dir: str, reg: dict) -> None:
    tmp = os.path.join(run_dir, REGISTRY + ".tmp")   # a fixed name the integrity record knows
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, indent=1, default=str)
    os.replace(tmp, os.path.join(run_dir, REGISTRY))


def gate_findings(result: dict) -> list[dict]:
    """Regressions (HIGH), unmet tests (their severity), an unreadable
    registry or a crashed evaluation (HIGH), missing tests (MEDIUM, one per
    critic)."""
    out = []
    if result.get("error"):
        out.append({"kind": "done_when_unavailable", "severity": "HIGH", "stage": "CRITIQUE",
                    "target_stage": "CRITIQUE", "scope_declarable": True,
                    "claim": f"The done_when tests could not be evaluated ({result['error'][:200]}); "
                             "no finding can be closed or checked for regressions by its test."})
    for r in result.get("regressed") or []:
        out.append({"kind": "done_when_regressed", "severity": "HIGH", "id": r["id"],
                    "target_stage": r["target_stage"], "stage": r["target_stage"], "scope_declarable": True,
                    "claim": (f"{r['id']} ({r['reviewer']}) passed its done_when test in round {r['passed_round']} "
                              f"and fails now: {r['detail'][:300]}. Something changed since; restore the fix. "
                              f"Finding: {r['claim'][:150]}")})
    for r in result.get("unmet") or []:
        out.append({"kind": "done_when_unmet", "severity": r["severity"], "id": r["id"],
                    "target_stage": r["target_stage"], "stage": r["target_stage"], "scope_declarable": True,
                    "claim": (f"{r['id']} ({r['reviewer']}) is marked resolved but its done_when test fails: "
                              f"{r['detail'][:300]}. Fix it so the test passes, or the critic withdraws the test "
                              f"(`done_when_withdrawn: <reason>`). Finding: {r['claim'][:150]}")})
    if result.get("invalid"):
        out.append({"kind": "done_when_registry_invalid", "severity": "HIGH", "stage": "CRITIQUE",
                    "target_stage": "CRITIQUE", "scope_declarable": True,
                    "claim": (f"done_when_registry.json has unreadable tests ({', '.join(result['invalid'][:10])}); "
                              "the gate cannot check those findings.")})
    for reviewer, ids in sorted((result.get("missing") or {}).items()):
        out.append({"kind": "critique_done_when_missing", "severity": "MEDIUM", "stage": "CRITIQUE",
                    "target_stage": "CRITIQUE",
                    "claim": (f"{reviewer}: {len(ids)} open HIGH/MEDIUM finding(s) without a valid done_when test "
                              f"({', '.join(ids[:10])}{', …' if len(ids) > 10 else ''}); they still block. "
                              f"Re-spawn {reviewer} to add the tests.")})
    return out


# ---------------------------------------------------------------------------
# CLI and self-test
# ---------------------------------------------------------------------------

def _load_critiques(run_dir: str) -> dict:
    out = {}
    for r in CRITICS:
        try:
            out[r] = yaml.safe_load(open(os.path.join(run_dir, r.replace("-", "_") + ".yaml"))) or {}
        except (OSError, yaml.YAMLError):
            continue
    return out


def check_cli(run_dir: str, ids: list[str]) -> int:
    """Read-only: frozen tests first, then the critiques' tests."""
    tests = {k: v["done_when"] for k, v in load_registry(run_dir)["tests"].items()}
    for _, b in _blockers(_load_critiques(run_dir)):
        tests.setdefault(str(b["id"]), b.get("done_when"))
    want = ids or sorted(tests)
    failing = 0
    for i in want:
        if i not in tests:
            print(f"{i}: no such finding with a test")
            failing += 1
            continue
        passes, detail = evaluate(tests[i], run_dir) if tests[i] is not None else (None, "no done_when test")
        print(f"{i}: {'PASS' if passes else 'FAIL' if passes is False else 'n/a'} — {detail}")
        failing += passes is not True
    return 1 if failing else 0


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        if not cond:
            fails.append(msg)

    with tempfile.TemporaryDirectory() as d:
        json.dump({"total_cost_usd": {"ci95": [412.0, 412.0], "mean": 412.0}, "n": 230},
                  open(os.path.join(d, "out.json"), "w"))
        open(os.path.join(d, "summary.md"), "w").write("The package averts 64M cases.\n")
        os.makedirs(os.path.join(d, "_truth"))
        ci = {"file": "out.json", "key": "total_cost_usd.ci95", "op": "width_gt", "value": 0}
        rng = {"file": "summary.md", "contains": r"re-anchored range \d+"}
        # DW1-DW4: forms and validation
        ok(problems([ci, rng]) == [] and mechanical([ci, rng]), "DW1 a list of runnable tests is valid")
        ok(problems({"judgement": "the critic reads the Summary"}) == []
           and not mechanical({"judgement": "x"}), "DW2 judgement is valid, not runnable")
        bad = [problems({"file": "/etc/passwd", "exists": True}), problems({"file": "../x", "exists": True}),
               problems({"file": "_truth/answers.json", "exists": True}), problems({"file": ".env", "exists": True}),
               problems({"file": "a.json", "key": "x", "op": "~"}), problems({"file": "a.md", "contains": "("}),
               problems({"file": "a.md"}), problems([]), problems("x"),
               problems([{"judgement": "x"}, ci])]
        ok(all(bad[:-1]) and bad[-1] == [] and problems([{"judgement": "a"}, {"judgement": "b"}]),
           f"DW3 unsafe paths, bad ops/regex, no form, empty or two judgements are invalid: {bad}")
        ok(evaluate({"file": "_truth/x", "exists": True}, d)[0] is None, "DW4 _truth is never read")
        name = os.path.basename(d)
        ok(evaluate({"file": f"runs/{name}/out.json", "exists": True}, d)[0] is True
           and evaluate({"file": f"{name}/out.json", "exists": True}, d)[0] is True,
           "DW4b a path written from the repo root (runs/<run>/...) reads the run's file")
        ok(evaluate({"file": f"runs/{name}/_truth/x", "exists": True}, d)[0] is None,
           "DW4c the repo-root prefix cannot reach _truth/")
        # DW5-DW8: evaluation
        ok(evaluate(ci, d)[0] is False and evaluate(rng, d)[0] is False, "DW5 both fail on the broken files")
        json.dump({"total_cost_usd": {"ci95": [371.0, 455.0]}, "n": 230}, open(os.path.join(d, "out.json"), "w"))
        open(os.path.join(d, "summary.md"), "w").write("64M cases (re-anchored range 51-77M).\n")
        p, det = evaluate([ci, rng], d)
        ok(p is True and "pass" in det, f"DW6 both pass after the fix: {det}")
        ok(evaluate({"file": "out.json", "key": "n", "op": "between", "value": [200, 300]}, d)[0] is True
           and evaluate({"file": "out.json", "key": "n", "op": ">", "value": 300}, d)[0] is False
           and evaluate({"file": "out.json", "key": "total_cost_usd.ci95.0", "op": "==", "value": 371}, d)[0]
           and evaluate({"file": "summary.md", "lacks": "TODO"}, d)[0] is True, "DW7 between, >, index, lacks")
        open(os.path.join(d, "alloc.csv"), "w").write("unit,package,cost\na,P3,10\nb,IRS,12\nc,P5,9\n")
        col = lambda **k: {"file": "alloc.csv", "column": k.pop("column", "package"), **k}  # noqa: E731
        ok(evaluate(col(op="==", value="IRS", rows="none"), d)[0] is False
           and evaluate(col(op="==", value="IRS", rows="any"), d)[0] is True
           and evaluate(col(column="cost", op="<=", value=12), d)[0] is True
           and evaluate(col(column="cost", op="<", value=12), d)[0] is False
           and evaluate(col(column="nope", op="==", value=1), d)[0] is False
           and evaluate(col(op="matches", value="^p[35]$", rows="any"), d)[0] is True
           and evaluate(col(op="matches", value="^p[35]$", min_rows=2), d)[0] is True
           and evaluate(col(op="matches", value="^p[35]$", min_rows=3), d)[0] is False
           and problems(col(op="==", value=1, min_rows=0))
           and evaluate(col(op="==", value="IRS", rows="all", where={"unit": "b"}), d)[0] is True
           and evaluate(col(op="==", value="IRS", rows="all", where={"cost": 9}), d)[0] is False
           and problems({"file": "a.csv", "column": "x", "op": "matches", "value": "("}) != []
           and problems(col(op="==", value=1, rows="most")), "DW7b CSV column forms: none / any / all, missing column")
        bad2 = [problems({"file": "a.md", "contains": ""}), problems({"file": "a.md", "lacks": None}),
                problems({"file": "a.md", "exists": "yes"}), problems({"file": "a.json", "key": "", "op": "==", "value": 1}),
                problems({"file": "a.json", "key": "x", "op": ">"}), problems({"file": "a.json", "key": "x", "op": ">", "value": "big"}),
                problems({"file": "a.csv", "column": "c", "op": "==", "value": 1, "min_rows": True}),
                problems({"file": "a.json", "key": "x", "op": "between", "value": ["a", 2]})]
        ok(all(bad2), f"DW3b empty regex, non-boolean exists, empty key, missing or non-numeric values, boolean min_rows: {bad2}")
        json.dump({"pair": [1, 2], "name": "P5", "n": 3}, open(os.path.join(d, "v.json"), "w"))
        ok(evaluate({"file": "v.json", "key": "pair", "op": "!=", "value": [1, 3]}, d)[0] is True
           and evaluate({"file": "v.json", "key": "name", "op": "!=", "value": 5}, d)[0] is True
           and evaluate({"file": "v.json", "key": "n", "op": "==", "value": 3.0}, d)[0] is True,
           "DW7f == / != on lists, strings and numbers do not raise")
        ok(evaluate(col(column="cost", agg="sum", op="<=", value=31), d)[0] is True
           and evaluate(col(column="cost", agg="sum", op="<=", value=30), d)[0] is False
           and evaluate(col(column="cost", agg="max", op="==", value=12), d)[0] is True
           and evaluate(col(column="cost", agg="count", op="==", value=1, where={"package": "IRS"}), d)[0] is True
           and problems(col(column="cost", agg="median", op="<", value=1))
           and evaluate(col(agg="count", op="==", value=1, where={"package": "IRS"}), d)[0] is True
           and evaluate(col(op="==", value="P3", where={"unit": "zz"}), d)[0] is False, "DW7c CSV aggregates")
        open(os.path.join(d, "blank.csv"), "w").write("unit,cost\na,\nb,\n")
        ok(evaluate({"file": "blank.csv", "column": "cost", "agg": "sum", "op": "<=", "value": 100}, d)[0] is False,
           "DW7e blank cells fail a numeric aggregate (missing output is not zero)")
        mixed = [{"file": "alloc.csv", "exists": True}, {"judgement": "the critic reads the caveat"}]
        ok(evaluate(mixed, d)[0] is None and not mechanical(mixed)
           and evaluate([{"file": "nope.csv", "exists": True}, {"judgement": "x"}], d)[0] is False,
           "DW7d runnable checks with a judgement: necessary, not sufficient")
        p, det = evaluate({"file": "out.json", "key": "missing.key", "op": "==", "value": 1}, d)
        ok(p is False and "KeyError" in det, f"DW8 a missing key fails, it does not raise: {det}")
        # DW9-DW14: registry, freezing, resolution, regression, missing tests
        open(os.path.join(d, "summary.md"), "w").write("The package averts 64M cases.\n")
        json.dump({"total_cost_usd": {"ci95": [412.0, 412.0]}}, open(os.path.join(d, "out.json"), "w"))
        crit = {"critique-redteam": {"blockers": [
            {"id": "R-005", "severity": "MEDIUM", "resolved": False, "claim": "no qualifier", "done_when": rng},
            {"id": "R-006", "severity": "HIGH", "resolved": False, "claim": "no test"}]},
            "critique-methods": {"blockers": [
                {"id": "M-001", "severity": "HIGH", "resolved": False, "claim": "zero CI", "done_when": ci}]},
            "critique-crossmodel": {"blockers": [{"id": "X-001", "severity": "HIGH", "resolved": False}]}}
        r1 = run(d, crit, 2)
        ok(r1["resolved"] == [] and r1["missing"] == {"critique-redteam": ["R-006"]}
           and os.path.exists(os.path.join(d, REGISTRY)), f"DW9 tests registered, the untested HIGH listed: {r1}")
        crit["critique-redteam"]["blockers"][0]["done_when"] = {"file": "summary.md", "contains": "never"}
        open(os.path.join(d, "summary.md"), "w").write("64M cases (re-anchored range 51-77M).\n")
        r2 = run(d, crit, 3)
        ok(r2["resolved"] == ["R-005"], f"DW10 the frozen test decides, not the critic's later edit: {r2['resolved']}")
        json.dump({"total_cost_usd": {"ci95": [371.0, 455.0]}}, open(os.path.join(d, "out.json"), "w"))
        r3 = run(d, crit, 4)
        ok(sorted(r3["resolved"]) == ["M-001", "R-005"] and not r3["regressed"],
           f"DW11 a passing test resolves an open HIGH: {r3['resolved']}")
        run(d, crit, 4)
        ok([h for h in load_registry(d)["tests"]["M-001"]["history"] if h[0] == 4] == [[4, True]]
           and load_registry(d)["tests"]["M-001"]["passed_round"] == 4, "DW12 a re-run in the same round is idempotent")
        crit["critique-methods"]["blockers"][0]["resolved"] = True
        json.dump({"total_cost_usd": {"ci95": [400.0, 400.0]}}, open(os.path.join(d, "out.json"), "w"))
        r5 = run(d, crit, 6)
        g = gate_findings(r5)
        ok([x["id"] for x in r5["regressed"]] == ["M-001"]
           and any(x["kind"] == "done_when_regressed" and x["severity"] == "HIGH" and "round 4" in x["claim"]
                   for x in g), f"DW13 a resolved finding whose test breaks reopens as HIGH: {g}")
        ok(any(x["kind"] == "critique_done_when_missing" and "R-006" in x["claim"] and x["severity"] == "MEDIUM"
               for x in g), "DW14 one MEDIUM per critic names the untested ids")
        ok("X-001" not in json.dumps(r5), "DW15 the cross-model critic (a script) is not asked for tests")
        # DW19-DW22 (Codex review): same-round regression, unmet tests, withdrawal, bad registry
        json.dump({"total_cost_usd": {"ci95": [371.0, 455.0]}}, open(os.path.join(d, "out.json"), "w"))
        run(d, crit, 6)
        json.dump({"total_cost_usd": {"ci95": [400.0, 400.0]}}, open(os.path.join(d, "out.json"), "w"))
        ok([x["id"] for x in run(d, crit, 6, record=False)["regressed"]] == ["M-001"],
           "DW19 a test that passed earlier in the same round and fails now is a regression")
        crit["critique-redteam"]["blockers"].append(
            {"id": "R-007", "severity": "HIGH", "resolved": True, "claim": "said fixed",
             "done_when": {"file": "never.csv", "exists": True}})
        r7 = run(d, crit, 6, record=False)
        ok([x["id"] for x in r7["unmet"]] == ["R-007"]
           and any(g["kind"] == "done_when_unmet" and g["severity"] == "HIGH" for g in gate_findings(r7)),
           f"DW20 resolved by the critic while its runnable test fails: blocking at its severity: {r7['unmet']}")
        crit["critique-redteam"]["blockers"][-1]["done_when_withdrawn"] = "the file is not needed for the fix"
        r8 = run(d, crit, 7)
        ok(not r8["unmet"] and "withdrawn in round 7" in r8["status"]["R-007"]["detail"]
           and load_registry(d)["tests"]["R-007"]["withdrawn"]["round"] == 7,
           "DW21 a critic's withdrawal is recorded, with its round and reason")
        ok(not run(d, crit, 7, record=False, exclude={"R-007", "M-001"})["regressed"],
           "DW22 findings outside the brief are neither registered nor reopened")
        reg_bad = load_registry(d)
        reg_bad["tests"]["Z-1"] = {}
        _save(d, reg_bad)
        rb = run(d, crit, 7, record=False)
        ok(rb["invalid"] == ["Z-1"] and any(g["kind"] == "done_when_registry_invalid" for g in gate_findings(rb)),
           "DW23 an unreadable registry entry is a HIGH finding, not skipped")
        del reg_bad["tests"]["Z-1"]
        _save(d, reg_bad)
        json.dump({"total_cost_usd": {"ci95": [400.0, 400.0]}}, open(os.path.join(d, "out.json"), "w"))
        before = json.dumps(load_registry(d), sort_keys=True)
        r6 = run(d, crit, 9, record=False)
        ok(json.dumps(load_registry(d), sort_keys=True) == before and r6["regressed"],
           "DW16 record=False evaluates without saving")
        st = r6["status"]
        ok(st["M-001"]["mechanical"] and st["M-001"]["passes"] is False, f"DW17 status per test: {st}")
        ok(check_cli(d, ["R-005"]) == 0 and check_cli(d, ["M-001"]) == 1 and check_cli(d, ["Z-9"]) == 1,
           "DW18 the check command exits 0 only when every named test passes")
    if fails:
        print("SELF-TEST FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", choices=("check",))
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if a.cmd != "check" or not a.run_dir:
        ap.error("usage: done_when.py check <run_dir> [ID ...]")
    return check_cli(a.run_dir, a.ids)


if __name__ == "__main__":
    sys.exit(main())
