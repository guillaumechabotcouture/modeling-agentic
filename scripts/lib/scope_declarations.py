"""Phase 21 β: scope_declaration.yaml normalization (moved here in Phase 49 α).

    python3 scripts/lib/scope_declarations.py --self-test

Shared by scripts/validate_critique_yaml.py (budget, rule 3a, pre-mortem,
not_applicable) and scripts/declaration_tests.py (Phase 49 α: a declaration
of a HIGH finding cites a decision test). Moved verbatim; the names lost their
leading underscore and the validator imports them under the old names.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from typing import Any, Optional

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.safe_io import safe_load_mapping  # noqa: E402

#
# Real runs used at least eight different top-level keys for their
# declarations (`declarations`, `scope_declarations`, `unresolved_blockers`,
# `unresolved_high_blockers`, `acknowledged_limitations`, ...), referenced
# blockers via `blocker_id`, lists of ids, or ids embedded in prose, and
# marked some entries `status: RESOLVED`. The Phase 19 β budget counted only
# `declarations:` entries with `id`/`claim`, so any other shape burned zero
# budget; the pre-mortem check accepted any substring anywhere in the file.
# One normalizer now feeds both.

BLOCKER_ID_RE = re.compile(r"\b[A-Z][A-Z0-9]{0,5}-\d{2,4}\b")
STRUCTURED_ID_FIELDS = ("blocker_id", "blocker_ids", "blockers", "covers",
                        "concern_id", "concern_ids", "ids", "acknowledges",
                        "blocker")
JUSTIFICATION_FIELDS = ("why_unresolved", "justification", "rationale",
                        "claim", "reason", "impact_on_conclusions",
                        "description")
# Gate kinds that carry a critic's finding under another name (Phase 39; moved
# from validate_critique_yaml.py in Phase 49 α): never declared away early, and
# outside the decision-test requirement (a critic blocker is the critic's).
CRITIC_DERIVED_PREFIXES = ("done_when_", "critique_", "claim_overclaimed", "sufficiency", "crossmodel")
NON_DECLARATION_KEYS = frozenset({"budget", "version", "run_dir",
                                  "generated_at", "declared_at"})


def load(run_dir: str) -> tuple[Optional[dict], list[dict], list[dict]]:
    """Return (doc, entries, findings) for scope_declaration.yaml.

    `entries` is every mapping found in any top-level list (except the
    `budget` block and metadata keys), plus any top-level mapping value
    that itself looks like a declaration (has an id field). Entries with
    `status: RESOLVED` are dropped — they declare nothing.
    """
    path = os.path.join(run_dir, "scope_declaration.yaml")
    doc, findings = safe_load_mapping(path, "scope_declaration", stage="GATE",
                                      display_path="scope_declaration.yaml")
    if doc is None:
        return None, [], findings
    entries: list[dict] = []
    for key, val in doc.items():
        if key in NON_DECLARATION_KEYS:
            continue
        items = val if isinstance(val, list) else (
            [dict(val, id=val.get("id", key))]
            if isinstance(val, dict) else [])
        for e in items:
            if isinstance(e, dict):
                if str(e.get("status") or "").strip().upper() == "RESOLVED":
                    continue
                entries.append(e)
            elif isinstance(e, str) and e.strip():
                entries.append({"claim": e})
            else:
                entries.append({})
    return doc, entries, findings


def strings_in(obj: Any) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in strings_in(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in strings_in(v)]
    return []


def entry_declared_ids(entry: dict, structured_only: bool) -> set[str]:
    """Blocker ids an entry declares. Structured fields always count;
    with structured_only=False, ids mentioned anywhere in the entry's
    text count too (one entry listing 20 ids declares 20 blockers).
    The entry's own `id` (e.g. SD-001) is not a blocker reference unless
    it is the only identifier present."""
    ids: set[str] = set()
    for f in STRUCTURED_ID_FIELDS:
        v = entry.get(f)
        for x in (v if isinstance(v, list) else [v]):
            if isinstance(x, (str, int)) and str(x).strip():
                ids.add(str(x).strip())
    if not structured_only:
        own = str(entry.get("id") or "")
        for txt in strings_in({k: v for k, v in entry.items() if k != "id"}):
            ids.update(m for m in BLOCKER_ID_RE.findall(txt) if m != own)
    return ids


def entry_has_justification(entry: dict) -> bool:
    return any(str(entry.get(f) or "").strip() for f in JUSTIFICATION_FIELDS)


# ---------------------------------------------------------------------------
# Inlined declaration-test & owner-exemption helpers
# (formerly declaration_tests.py + stall_rule.py + mechanism_manifest.py)
# ---------------------------------------------------------------------------

FIELD = "decision_test"
PRODUCERS = ("agent", "harness")
_CRITIC_ID = re.compile(r"^[A-Z][A-Z0-9]{0,5}-\d{2,4}[a-z]?$")
_KEY_FIELDS = ("key", "artifact")
REGRET_TOL = 1e-9


def finding_key(v: dict) -> str:
    """The instance key the producing check set ('' when it set none)."""
    for f in _KEY_FIELDS:
        x = v.get(f)
        if isinstance(x, (list, tuple, set)):
            names = sorted({str(s).strip() for s in x if str(s).strip()})
            if names:
                return f"n={len(names)}:" + hashlib.sha1(",".join(names).encode()).hexdigest()[:8]
        elif isinstance(x, (str, int)) and not isinstance(x, bool) and str(x).strip():
            return str(x).strip()
    return ""


def _base(kind: str) -> str:
    return kind[:-len("_persistent")] if kind.endswith("_persistent") else kind


def critic_derived(v: dict) -> bool:
    kind = str(v.get("kind") or "")
    base = _base(kind)
    return (kind.startswith(CRITIC_DERIVED_PREFIXES) or base.startswith(CRITIC_DERIVED_PREFIXES)
            or bool(_CRITIC_ID.match(base))
            or str(v.get("source") or v.get("reviewer") or "").startswith("critique"))


def key_of(v: dict) -> str:
    """The finding's instance key, normalized as finding_key records it."""
    k = finding_key(v)
    if k:
        return k
    s = v.get("stall_key")
    return str(s).strip() if isinstance(s, (str, int)) and not isinstance(s, bool) and str(s).strip() else ""


def identities(v: dict) -> set[str]:
    """Names a declaration may use for the finding: kind, kind:key, key (and the base kind's)."""
    kind, key = str(v.get("kind") or ""), key_of(v)
    out = {kind, _base(kind)}
    if key:
        out |= {key, f"{kind}:{key}", f"{_base(kind)}:{key}"}
    return {x for x in out if x}


def test_names(v: dict) -> set[str]:
    """What a result file's `finding:` may say: the instance when the finding has a key."""
    kind, key = str(v.get("kind") or ""), key_of(v)
    return {key, f"{kind}:{key}", f"{_base(kind)}:{key}"} if key else {kind, _base(kind)}


def _ident(v: dict) -> str:
    key = key_of(v)
    return f"{v.get('kind')}:{key}" if key else str(v.get("kind"))


def covering(entries: list[dict], v: dict) -> list[dict]:
    """The entries that declare this finding (with a justification)."""
    names = identities(v)
    return [e for e in entries if entry_has_justification(e) and entry_declared_ids(e, True) & names]


def _inside(run_dir: str, rel: str) -> Optional[str]:
    if not isinstance(rel, str) or not rel.strip():
        return None
    root = os.path.realpath(run_dir)
    p = os.path.realpath(os.path.join(root, rel.strip()))
    if not (p == root or p.startswith(root + os.sep)) or not os.path.isfile(p):
        return None
    return p


def _load_result(path: str):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return json.loads(text) if path.endswith(".json") else yaml.safe_load(text)


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _norm(t: str) -> str:
    return " ".join(str(t).lower().split())


def _test_result(run_dir: str, rel) -> tuple[Optional[dict], Optional[str]]:
    p = _inside(run_dir, rel) if isinstance(rel, str) else None
    if p is None:
        return None, f"names no existing result file (`sufficiency_test: {rel!r}`)"
    try:
        res = _load_result(p)
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        return None, f"has a result file that cannot be parsed ({exc})"
    if not isinstance(res, dict):
        return None, "has a result file that is not a mapping"
    if not isinstance(res.get("decision_changes"), bool):
        return None, "has a result file without a `decision_changes: true|false` verdict"
    if not res.get("varied") or not (isinstance(res.get("criterion"), str) and res["criterion"].strip()):
        return None, "has a result file that does not state what was `varied` and the decision `criterion`"
    return res, None


def _result_conflicts(rel: str, res: dict, label: str, restated) -> list[str]:
    out = []
    share, thr = _num(res.get("share_units_changed")), _num(res.get("share_threshold"))
    given = [k for k in ("share_units_changed", "share_threshold") if res.get(k) is not None]
    if given and (share is None or thr is None or not 0 <= share <= 1 or not 0 <= thr <= 1):
        out.append(f"`{rel}`: share_units_changed and share_threshold must both be fractions in [0, 1] when "
                   f"either is given (got {res.get('share_units_changed')!r}, {res.get('share_threshold')!r}).")

    def fired(r):
        crit = {"share": (_num(r.get("share_units_changed")), thr)}
        for name, vk, tk in (("regret", "regret_share", "regret_threshold"),
                             ("dollars", "dollars_share", "dollars_threshold")):
            if r.get(vk) is not None or res.get(tk) is not None:
                v, t = _num(r.get(vk)), _num(res.get(tk))
                if name == "regret" and r.get(vk) is None and t is not None and 0 <= t <= 1 and (
                        r.get("point_plan_feasible") is False
                        or (_num(r.get("reoptimised_objective")) is not None and _num(r["reoptimised_objective"]) <= 0
                            and _num(r.get("regret")) is not None)):
                    if _num(r.get("regret")) is not None and r.get("point_plan_feasible") is not False:
                        crit[name] = (_num(r["regret"]), REGRET_TOL * max(1.0, abs(_num(r["reoptimised_objective"]))))
                    continue
                if v is None or t is None or not 0 <= t <= 1:
                    out.append(f"`{rel}`: {vk} and {tk} must both be numbers ({tk} a fraction in [0, 1]) when "
                               f"either is given (got {r.get(vk)!r}, {res.get(tk)!r}), unless the point plan is "
                               "infeasible or the re-optimised objective is not positive.")
                    continue
                crit[name] = (v, t)
        k_ = {k: v > t for k, (v, t) in crit.items() if v is not None and t is not None}
        if "point_plan_feasible" in r:
            k_["infeasible"] = r["point_plan_feasible"] is not True
        return k_

    rows = res.get("results")
    if isinstance(rows, list) and rows and all(isinstance(x, dict) and "share_units_changed" in x for x in rows):
        per = [fired(x) for x in rows]
        known = {k: any(p.get(k, False) for p in per) for k in {k for p in per for k in p}}
    else:
        known = fired(res)
    if known and any(known.values()) != res["decision_changes"]:
        out.append(f"`{rel}`: criteria {', '.join(f'{k}: {f}' for k, f in known.items())} contradict "
                   f"decision_changes: {res['decision_changes']}.")
    tb = res.get("triggered_by")
    if tb is not None and (not isinstance(tb, list) or sorted(map(str, tb)) != sorted(k for k, f in known.items() if f)):
        out.append(f"`{rel}`: triggered_by {tb!r} is not the criteria its numbers fire "
                   f"({sorted(k for k, f in known.items() if f)}).")
    if isinstance(restated, bool) and restated != res["decision_changes"]:
        out.append(f"{label} restates decision_changes: {restated}, but its "
                   f"result file `{rel}` says {res['decision_changes']}.")
    return out


def load_test_result(run_dir: str, rel) -> tuple[Optional[dict], list[str]]:
    res, problem = _test_result(run_dir, rel)
    if problem:
        return None, [problem]
    conflicts = _result_conflicts(str(rel), res, "", None)
    return (None, conflicts) if conflicts else (res, [])


def _run_question_text(run_dir: str) -> str:
    path = os.path.join(run_dir, "metadata.json")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(meta, dict):
        return ""
    q = meta.get("question") or meta.get("research_question")
    return q if isinstance(q, str) else ""


def _scalars(node, _path: tuple = ()) -> list[str]:
    if isinstance(node, (dict, list)):
        if id(node) in _path:
            raise ValueError("recursive YAML alias")
        _path = _path + (id(node),)
    if isinstance(node, dict):
        return [s for k, v in node.items() for s in _scalars(k, _path) + _scalars(v, _path)]
    if isinstance(node, list):
        return [s for v in node for s in _scalars(v, _path)]
    return [str(node)] if isinstance(node, (str, int, float)) and not isinstance(node, bool) else []


def _decoded_owner_text(run_dir: str, by: str) -> str:
    if by == "question":
        return _run_question_text(run_dir)
    parts = []
    for rel in ("decision_brief.yaml", "decision_brief.md"):
        p = os.path.join(run_dir, rel)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                text = f.read()
            parts += _scalars(yaml.safe_load(text)) if rel.endswith(".yaml") else [text]
        except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError):
            continue
    return "\n".join(parts)


def owner_quote_found(run_dir: str, by: str, quote) -> bool:
    q = _norm(quote) if isinstance(quote, str) else ""
    return len(q.split()) >= 3 and q in _norm(_decoded_owner_text(run_dir, by))


def _amendments_ok_ids(ids) -> bool:
    return all(re.fullmatch(r"(?:OS|CF|[ACKQ])\d+", str(i)) for i in ids)


def _amendments(run_dir: str) -> tuple[list[dict], list[str]]:
    path = os.path.join(run_dir, "brief_amendments.yaml")
    if not os.path.exists(path):
        return [], []
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        return [], [f"brief_amendments.yaml does not parse: {str(e)[:100]}"]
    items = doc.get("amendments") if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        return [], ["brief_amendments.yaml: expected a list under `amendments:`"]
    errors, ok = [], []
    for am in items:
        if not isinstance(am, dict):
            errors.append("brief_amendments.yaml: an entry is not a mapping")
            continue
        item, aid = str(am.get("item") or ""), am.get("id") or "?"
        if not _amendments_ok_ids([item]):
            errors.append(f"{aid}: only framing items (A, C, K, OS, CF, Q ids) can be amended in-run, not {item or '?'}")
        elif len(str(am.get("reason") or "").strip()) < 20 or not str(am.get("change") or "").strip():
            errors.append(f"{aid}: needs `change` and a `reason`")
        else:
            ok.append(am)
    return ok, errors


def amendment_names(run_dir: str, aid, fid: str) -> bool:
    if not isinstance(aid, str) or not aid.strip():
        return False
    ok, _errors = _amendments(run_dir)
    for am in ok:
        if str(am.get("id", "")).strip() == aid.strip():
            text = f"{am.get('change', '')} {am.get('reason', '')}"
            return re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(fid) + r"(?![A-Za-z0-9_-]|\.[A-Za-z0-9_])", text, re.I) is not None
    return False


def declaration_test(run_dir: str, entry: dict) -> tuple[Optional[dict], Optional[str]]:
    if not isinstance(entry, dict) or FIELD not in entry:
        return None, None
    rel = entry.get(FIELD)
    if not isinstance(rel, str) or not rel.strip():
        return None, f"`{FIELD}:` must be one path to a result file (got {rel!r:.80})"
    res, problems = load_test_result(run_dir, rel.strip())
    if res is None:
        return None, f"`{FIELD}: {rel.strip()}` " + "; ".join(problems)
    bad = []
    if not (isinstance(res.get("finding"), str) and res["finding"].strip()):
        bad.append("`finding:` (the declared finding's kind:key or key)")
    rec = res.get("recommendation")
    if not (isinstance(rec, dict) and all(isinstance(rec.get(k), str) and rec[k].strip()
                                          for k in ("with", "without"))):
        bad.append("`recommendation: {with: ..., without: ...}` (the recommendation as delivered, and with the "
                   "varied quantity at its target)")
    if res.get("produced_by", "agent") not in PRODUCERS:
        bad.append(f"`produced_by:` one of {', '.join(PRODUCERS)} (got {res.get('produced_by')!r:.40})")
    if bad:
        return None, f"`{FIELD}: {rel.strip()}` needs " + " and ".join(bad)
    return res, None


def _exemption(run_dir: str, entry: dict, names: set[str]) -> tuple[Optional[str], Optional[str]]:
    by = str(entry.get("specified_by") or "").strip().lower()
    am = entry.get("owner_amendment")
    if not by and am is None:
        return None, None
    if by:
        if by not in ("question", "brief"):
            return None, f"`specified_by: {entry.get('specified_by')!r}` must be `question` or `brief`"
        if not owner_quote_found(run_dir, by, entry.get("quote")):
            return None, (f"`specified_by: {by}` but its `quote` ({str(entry.get('quote'))[:100]!r}) is not found "
                          f"verbatim (3+ words) in the run's {by}")
        return f"specified by the {by}: '{' '.join(str(entry.get('quote')).split())}'", None
    if not any(amendment_names(run_dir, am, n) for n in sorted(names)):
        return None, (f"`owner_amendment: {am!r}` is not a valid entry of brief_amendments.yaml whose change or "
                      f"reason names the finding ({', '.join(sorted(names))})")
    return f"owner amendment {str(am).strip()}", None


def exemption(run_dir: str, entry: dict) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    how, problem = _exemption(run_dir, entry, entry_declared_ids(entry, True))
    return None if problem else how


def entry_status(run_dir: str, entry: dict, v: dict) -> tuple[str, str, Optional[dict]]:
    names = test_names(v)
    res, problem = declaration_test(run_dir, entry)
    if problem:
        return "invalid", problem, None
    if res is not None:
        if str(res["finding"]).strip() not in names:
            return "invalid", (f"`{entry[FIELD]}` is the test of `{res['finding']}`, not of this finding (name it "
                               f"as one of {', '.join(sorted(names))}): a result file belongs to one finding"), None
        if res["decision_changes"]:
            return "changes", str(entry[FIELD]).strip(), res
        return "tested", str(entry[FIELD]).strip(), res
    how, problem = _exemption(run_dir, entry, identities(v))
    if problem:
        return "invalid", problem, None
    if how:
        return "exempt", how, None
    return "untested", "", None


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        print(("  ok   " if c else "  FAIL ") + m)
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d:
        doc, entries, f = load(d)
        ok(doc is None and entries == [], "SD1 no file: no entries")
        with open(os.path.join(d, "scope_declaration.yaml"), "w") as fh:
            fh.write("declarations:\n- id: SD-1\n  blocker_id: benchmark_drift\n  why_unresolved: x\n"
                     "- id: SD-2\n  status: RESOLVED\n  blocker_id: y\n"
                     "- just a string R-001\n"
                     "budget: {cap: 5}\n")
        doc, entries, f = load(d)
        ok(len(entries) == 2 and entries[1] == {"claim": "just a string R-001"},
           f"SD2 RESOLVED and budget dropped, strings kept: {entries}")
        ok(entry_declared_ids(entries[0], True) == {"benchmark_drift"}, "SD3 structured ids")
        ok(entry_declared_ids(entries[1], True) == set() and entry_declared_ids(entries[1], False) == {"R-001"},
           "SD4 prose ids count only when structured_only=False")
        ok(entry_has_justification(entries[0]) and not entry_has_justification({"id": "SD-9"}),
           "SD5 justification fields")
        with open(os.path.join(d, "scope_declaration.yaml"), "w") as fh:
            fh.write("declarations: [unclosed\n")
        doc, entries, f = load(d)
        ok(doc is None and bool(f) and f[0]["severity"] == "HIGH", f"SD6 unparseable file fails closed: {f}")
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models", "decision_tests"))
        with open(os.path.join(d, "models", "decision_tests", "t.yaml"), "w") as fh:
            yaml.safe_dump({"finding": "benchmark_drift:B1", "varied": "x", "criterion": "c",
                            "decision_changes": False, "recommendation": {"with": "a", "without": "a"}}, fh)
        e = {"id": "SD-1", "blocker_id": "benchmark_drift:B1", "why_unresolved": "w",
             "decision_test": "models/decision_tests/t.yaml"}
        st, detail, _ = entry_status(d, e, {"kind": "benchmark_drift", "key": "B1"})
        ok(st == "tested" and detail == "models/decision_tests/t.yaml", f"SD7 entry_status tested: {st} {detail}")
        ok(finding_key({"key": ["b", "a"]}).startswith("n=2:"), "SD8 finding_key normalizes list")
        with open(os.path.join(d, "decision_brief.yaml"), "w") as fh:
            yaml.safe_dump({"out_of_scope": [{"id": "OS1", "text": "out of scope topic"}]}, fh)
        ok(exemption(d, {"id": "SD-2", "blocker_id": "B1", "specified_by": "brief",
                         "quote": "out of scope topic"}) == "specified by the brief: 'out of scope topic'",
           "SD9 brief quote exemption")
        with open(os.path.join(d, "brief_amendments.yaml"), "w") as fh:
            yaml.safe_dump({"amendments": [{"id": "AM-1", "item": "OS1", "change": "accept B1",
                                            "reason": "owner accepted this limitation for the run"}]}, fh)
        ok(exemption(d, {"id": "SD-3", "blocker_id": "B1", "owner_amendment": "AM-1"}) == "owner amendment AM-1",
           "SD10 owner_amendment exemption")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(2)
