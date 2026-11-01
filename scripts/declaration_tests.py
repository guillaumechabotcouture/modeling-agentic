#!/usr/bin/env python3
"""Phase 49 α (A20): a scope declaration of a HIGH finding needs a decision test.

    python3 scripts/declaration_tests.py <run_dir> [--round N]   # the current declarations
    python3 scripts/declaration_tests.py --self-test

E7 attempt 8's gate flagged its main weaknesses HIGH every round: the
allocation did not hold out of sample (leave-one-archetype-out worst-fold rank
correlation 0.35, package concordance 33%) while the question asks for an
assignment per unit; the model's ITN x IRS interaction (OR 0.94) and IRS cost
per DALY (about 330) were far from their benchmarks (2.43, about 25) while the
plan recommends no IRS. The lead declared all three in scope_declaration.yaml
with a reason each, and DECLARE_SCOPE counted as success. Nothing tested
whether the decision would change if the finding were fixed. D1 (mechanisms)
and D9 (decision-layer features, both in `models/mechanism_manifest.yaml`)
already require that test: re-run the decision with the simplification
varied and record whether it changes. Scope declarations now do too.

What needs a test
-----------------
A HIGH gate finding (a MEDIUM escalated to HIGH, `<kind>_persistent`,
included) that is scope-declarable and is covered by a scope_declaration.yaml
entry (one of the entry's structured ids is the finding's kind, `kind:key` or
key; for `<kind>_persistent`, also the base kind). Not: critic blockers and
the gate kinds that carry them (`done_when_*`, `critique_*`, `<D-014>_persistent`,
...), required-artifact accept blockers (`not_applicable:` covers those), and
the classes in `.claude/orchestration/scope_declaration_policy.yaml`
(`decision_test:`), each of which has no model quantity to vary. At the
post-WRITE gate, such a finding that no entry covers is flagged too: leaving a
finding undeclared must not be easier than declaring it.

The entry cites the result:

    declarations:
      - id: SD-007
        blocker_id: allocation_unstable
        why_unresolved: "..."
        decision_test: models/decision_tests/allocation_unstable.yaml

The result file has the D1/D9 format (loaded by
`mechanism_manifest.load_test_result`) and names the finding it belongs to:

    finding: allocation_unstable:allocation_robustness   # kind, kind:key or key
    varied: "the held-out archetype's parameters set to its own survey fit"
    criterion: "package changes in more than 10% of units, or the headline
                leaves its 95% interval"
    decision_changes: false
    recommendation:
      with: "<the recommendation as delivered>"
      without: "<the recommendation with the varied quantity at its target>"
    # optional, checked when both are given:
    share_units_changed: 0.04
    share_threshold: 0.10
    produced_by: agent          # optional: agent (default) | harness

The schema is only inputs and outputs, so the harness can later compute these
files itself (stage-machine S5: re-solve through outcome_fn + toolkit/alloc
where those contracts exist); the gate logic does not change when it does.

A finding with a key needs a test naming that key (`benchmark_drift:B15` or
`B15`): one test per benchmark, not one per kind. Keys are normalized as A17
records them (`stall_rule.finding_key`: a parameter set is `n=<size>:<hash>`).
A `<kind>_persistent` escalation stands for each keyed instance of its base
finding: it is covered when every instance is (one test per instance).

All gate findings are checked (spec-compliance, rigor and registry), once they
are assembled (`validate_critique_yaml._apply_declaration_tests`). A17 counts a
declaration of a stalled finding, whatever its severity, only when the covering
entry has a passing test or an exemption (`declaration_counts`).

Exemptions on the entry (verified, as in D1/D9): `specified_by: question|brief`
with a verbatim `quote:` of 3+ words from the run question or signed brief,
or `owner_amendment: AM-n`, a brief amendment whose change or reason names
the finding. A data-availability limit alone is not an exemption: see the
policy file.

Findings (none scope-declarable):
  scope_declaration_untested        HIGH  covered by a declaration with neither a
                                          test nor an exemption (or, post-WRITE,
                                          by no declaration)
  scope_declaration_test_invalid    HIGH  the cited result file is missing,
                                          unreadable, incomplete, contradicts itself,
                                          or names another finding; or an exemption
                                          does not check out (fail closed)
  declared_finding_changes_decision HIGH  the test finds the decision changes: the
                                          finding must be fixed, not declared

Phase 49 β: a finding that bears on the primary decision is put to the owner
(scripts/scope_signoff.py, run first, marks it); for it only the untested
outcome is replaced by the owner's answer. A test that shows the decision
changes, or an unusable one, still fails here.

A declaration whose test passes stands; the report must cite the test
(`tested_declarations(run_dir)` exposes the data for that check). A stall
diagnosis with `decision: declare` (scripts/stall_rule.py) needs the same test
(`diagnosis_problem`).
"""
from __future__ import annotations

import os
import re
import sys
from typing import Callable, Optional

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from lib import scope_declarations as sd  # noqa: E402

FIELD = "decision_test"
UNTESTED = "scope_declaration_untested"
INVALID = "scope_declaration_test_invalid"
CHANGES = "declared_finding_changes_decision"
KINDS = (UNTESTED, INVALID, CHANGES)
# Who wrote the result file. The schema is inputs and outputs only, so the
# harness can write the same file itself (stage-machine S5: re-solve through
# outcome_fn + toolkit/alloc where those contracts exist) with no gate change.
PRODUCERS = ("agent", "harness")
POLICY = os.path.join(os.path.dirname(HERE), ".claude", "orchestration", "scope_declaration_policy.yaml")
_POLICY_KEYS = ("exempt_stages", "exempt_prefixes")
_CRITIC_ID = re.compile(r"^[A-Z][A-Z0-9]{0,5}-\d{2,4}[a-z]?$")
_HOWTO = ("Re-run the decision with the finding's quantity set to its target (the benchmark's value, the held-out "
          "fold's own parameters, the trial effect) and write a result file with `finding:` (kind:key), `varied`, "
          "`criterion`, `decision_changes: true|false` and `recommendation: {with: ..., without: ...}`; cite it on the "
          "entry as `decision_test: <path>`.")


def _mm():
    from lib.siblings import load_private
    return load_private("mechanism_manifest", "_ma_p49_")


def load_policy(path: str = POLICY) -> dict:
    """The `decision_test:` exemption classes. Raises ValueError when the file
    is malformed (the gate's guard turns that into a HIGH crash finding)."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        raise ValueError(f"{os.path.basename(path)} cannot be read: {e}") from e
    pol = doc.get("decision_test") if isinstance(doc, dict) else None
    if not isinstance(pol, dict):
        raise ValueError(f"{os.path.basename(path)} needs a `decision_test:` mapping")
    out = {}
    for k in _POLICY_KEYS:
        v = pol.get(k, [])
        if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            raise ValueError(f"{os.path.basename(path)}: decision_test.{k} must be a list of strings")
        out[k] = tuple(x.strip() for x in v)
    # Codex review (round 2): named kinds with a reason each, no suffix classes.
    kinds = pol.get("exempt_kinds", {})
    if not isinstance(kinds, dict) or not all(isinstance(k, str) and isinstance(r, str) and r.strip()
                                              for k, r in kinds.items()):
        raise ValueError(f"{os.path.basename(path)}: decision_test.exempt_kinds must map each kind to its reason")
    if "exempt_suffixes" in pol:
        raise ValueError(f"{os.path.basename(path)}: decision_test.exempt_suffixes is not supported; name each kind")
    out["exempt_kinds"] = dict(kinds)
    return out


def _base(kind: str) -> str:
    return kind[:-len("_persistent")] if kind.endswith("_persistent") else kind


def critic_derived(v: dict) -> bool:
    kind = str(v.get("kind") or "")
    base = _base(kind)
    return (kind.startswith(sd.CRITIC_DERIVED_PREFIXES) or base.startswith(sd.CRITIC_DERIVED_PREFIXES)
            or bool(_CRITIC_ID.match(base))
            or str(v.get("source") or v.get("reviewer") or "").startswith("critique"))


def kind_exempt(kind: str, policy: dict) -> bool:
    """The kind (or a persistent escalation's base) is in an exempt class by name."""
    for k in {kind, _base(kind)}:
        if k in policy["exempt_kinds"] or k.startswith(policy["exempt_prefixes"]):
            return True
    return False


def _sr():
    from lib.siblings import load_private
    return load_private("stall_rule", "_ma_p49_")


def key_of(v: dict) -> str:
    """The finding's instance key, normalized as A17 records it (stall_rule.finding_key:
    a parameter set is `n=<size>:<hash>`), so a test can name the identity the gate printed."""
    k = _sr().finding_key(v)
    if k:
        return k
    s = v.get("stall_key")
    return str(s).strip() if isinstance(s, (str, int)) and not isinstance(s, bool) and str(s).strip() else ""


def instances(v: dict, current: list[dict]) -> list[dict]:
    """The instances a finding stands for. A `<kind>_persistent` escalation with no key
    of its own stands for every keyed instance of its base kind the gate holds (Codex
    review, round 2: C1 and C2): it is covered when each of them is."""
    kind = str(v.get("kind") or "")
    if not kind.endswith("_persistent") or key_of(v):
        return [v]
    keys = sorted({key_of(x) for x in current if x.get("kind") == _base(kind)} - {""})
    return [dict(v, key=k) for k in keys] or [v]


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


def needs_test(v: dict, current: list[dict], policy: dict,
               non_declarable: Callable[[str, object], bool]) -> bool:
    if v.get("severity") != "HIGH" or v.get("accept_blocker") or critic_derived(v):
        return False
    kind = str(v.get("kind") or "")
    if not kind or kind in KINDS or non_declarable(kind, v.get("scope_declarable")):
        return False
    if kind_exempt(kind, policy):
        return False
    stage = str(v.get("stage") or "").upper()
    if kind.endswith("_persistent"):
        # A gate kind's escalation is judged by its base finding, when the gate still sees it.
        base = [x for x in current if x.get("kind") == _base(kind)]
        stage = str(base[0].get("stage") or "").upper() if base else ""
    return stage not in policy["exempt_stages"]


def declaration_test(run_dir: str, entry: dict) -> tuple[Optional[dict], Optional[str]]:
    """The decision-test result an entry cites: (result, None) when usable,
    (None, problem) when `decision_test:` is given but unusable, (None, None)
    when the entry cites none. The result's `finding` is not matched here."""
    if not isinstance(entry, dict) or FIELD not in entry:
        return None, None
    rel = entry.get(FIELD)
    if not isinstance(rel, str) or not rel.strip():
        return None, f"`{FIELD}:` must be one path to a result file (got {rel!r:.80})"
    res, problems = _mm().load_test_result(run_dir, rel.strip())
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
    """(how the entry is exempt, problem) from specified_by/quote or owner_amendment."""
    by = str(entry.get("specified_by") or "").strip().lower()
    am = entry.get("owner_amendment")
    if not by and am is None:
        return None, None
    mm = _mm()
    if by:
        if by not in ("question", "brief"):
            return None, f"`specified_by: {entry.get('specified_by')!r}` must be `question` or `brief`"
        if not mm.owner_quote_found(run_dir, by, entry.get("quote")):
            return None, (f"`specified_by: {by}` but its `quote` ({str(entry.get('quote'))[:100]!r}) is not found "
                          f"verbatim (3+ words) in the run's {by}")
        return f"specified by the {by}: '{' '.join(str(entry.get('quote')).split())}'", None
    if not any(mm.amendment_names(run_dir, am, n) for n in sorted(names)):
        return None, (f"`owner_amendment: {am!r}` is not a valid entry of brief_amendments.yaml whose change or "
                      f"reason names the finding ({', '.join(sorted(names))})")
    return f"owner amendment {str(am).strip()}", None


def exemption(run_dir: str, entry: dict) -> Optional[str]:
    """The reason a declaration needs no decision test, as the gate verifies it
    ("specified by the brief: '<quote>'", "owner amendment AM-n"), else None. The
    amendment must name one of the ids the entry declares. For the report check."""
    if not isinstance(entry, dict):
        return None
    how, problem = _exemption(run_dir, entry, sd.entry_declared_ids(entry, True))
    return None if problem else how


def entry_status(run_dir: str, entry: dict, v: dict) -> tuple[str, str, Optional[dict]]:
    """(status, detail, result) of one declaration for one finding:
    tested | exempt | changes | invalid | untested."""
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


def covering(entries: list[dict], v: dict) -> list[dict]:
    """The entries that declare this finding (with a justification)."""
    names = identities(v)
    return [e for e in entries if sd.entry_has_justification(e) and sd.entry_declared_ids(e, True) & names]


def _ident(v: dict) -> str:
    key = key_of(v)
    return f"{v.get('kind')}:{key}" if key else str(v.get("kind"))


def _finding(kind: str, v: dict, claim: str, detail: dict) -> dict:
    return {"kind": kind, "severity": "HIGH", "stage": "GATE", "target_stage": "MODEL",
            "scope_declarable": False, "key": _ident(v), "declaration_test": detail, "claim": claim}


def gate_findings(run_dir: str, round_n: Optional[int] = None, current: Optional[list] = None,
                  post_write: bool = False,
                  non_declarable: Callable[[str, object], bool] = lambda k, e: e is False) -> list[dict]:
    """Findings for each HIGH gate finding in `current` (this gate's findings so
    far) that a scope declaration covers without a usable decision test."""
    current = [v for v in (current or []) if isinstance(v, dict)]
    policy = load_policy()
    _doc, entries, problems = sd.load(run_dir)
    if problems:
        return []        # an unreadable scope_declaration.yaml is already HIGH (scope_declaration_invalid)
    out, seen = [], set()
    todo = [v for v0 in current if needs_test(v0, current, policy, non_declarable) for v in instances(v0, current)]
    for v in todo:
        ident = _ident(v)
        if ident in seen:
            continue
        seen.add(ident)
        cover = covering(entries, v)
        # Phase 49 β: a finding the owner decides (scripts/scope_signoff.py marks it) needs no
        # decision test of its own to be declared: the owner sees the test, or that there is none.
        owner = bool(v.get("owner_signoff") or v.get("owner_signed") or v.get("owner_declined"))
        if not cover:
            if post_write and not owner:
                out.append(_finding(UNTESTED, v, (
                    f"`{ident}` is open at the post-WRITE gate and no scope_declaration.yaml entry covers it. A run "
                    f"may end with it only as a declared limitation whose decision test shows the decision does not "
                    f"change. Declare it (blocker_id: {ident}) with `decision_test:`, or fix it. " + _HOWTO),
                    {"finding": ident, "status": "undeclared"}))
            continue
        statuses = [(e, *entry_status(run_dir, e, v)) for e in cover]
        eid = lambda e: str(e.get("id") or "(no id)")  # noqa: E731
        changes = [s for s in statuses if s[1] == "changes"]
        invalid = [s for s in statuses if s[1] == "invalid"]
        if changes:
            e, _, rel, res = changes[0]
            out.append(_finding(CHANGES, v, (
                f"`{ident}` is declared in {eid(e)}, but its decision test (`{rel}`) finds that the decision "
                f"changes ({str(res.get('criterion'))[:160]}; with: {str(res['recommendation']['with'])[:120]}; "
                f"without: {str(res['recommendation']['without'])[:120]}). A finding that changes the decision "
                "cannot be declared: fix it in the next round."),
                {"finding": ident, "entry": eid(e), "status": "changes", FIELD: rel}))
        elif invalid:
            e, _, why, _ = invalid[0]
            out.append(_finding(INVALID, v, (
                f"`{ident}` is declared in {eid(e)}, and its decision test or exemption is not usable: {why}. "
                "It does not count until fixed. " + _HOWTO),
                {"finding": ident, "entry": eid(e), "status": "invalid", "problem": why}))
        elif not owner and not any(s[1] in ("tested", "exempt") for s in statuses):
            out.append(_finding(UNTESTED, v, (
                f"`{ident}` (HIGH) is declared in {', '.join(eid(s[0]) for s in statuses)} with no decision test. "
                "A declared HIGH finding needs evidence that the decision does not depend on it. " + _HOWTO
                + " Exempt only when the question or signed brief scopes it out (`specified_by` + verbatim `quote`) "
                  "or a brief amendment names it (`owner_amendment: AM-n`)."),
                {"finding": ident, "entry": eid(statuses[0][0]), "status": "untested"}))
    return out


def tested_declarations(run_dir: str) -> list[dict]:
    """Every scope_declaration.yaml entry whose `decision_test:` loads: the data
    the report must cite (entry id, the finding, the file, the verdict)."""
    _doc, entries, _ = sd.load(run_dir)
    out = []
    for e in entries:
        res, _problem = declaration_test(run_dir, e)
        if res is None:
            continue
        out.append({"entry_id": str(e.get("id") or ""), "finding": str(res["finding"]).strip(),
                    FIELD: str(e[FIELD]).strip(), "decision_changes": res["decision_changes"],
                    "criterion": res.get("criterion"), "varied": res.get("varied"),
                    "recommendation": res.get("recommendation")})
    return out


def declaration_counts(run_dir: str, v: dict, current: Optional[list] = None) -> bool:
    """For A17 (scripts/stall_rule.py): a scope declaration clears a stalled finding,
    of any severity, only when a covering entry has a passing decision test or an
    exemption, or the finding is in an exempt class by kind."""
    try:
        if kind_exempt(str(v.get("kind") or ""), load_policy()):
            return True
    except ValueError:
        return False
    if v.get("owner_signed") or v.get("owner_signoff"):   # Phase 49 β: the owner decides it
        return True
    _doc, entries, problems = sd.load(run_dir)
    if problems:
        return False
    cur = [x for x in (current or []) if isinstance(x, dict)]
    return all(any(entry_status(run_dir, e, u)[0] in ("tested", "exempt") for e in covering(entries, u))
               for u in instances(v, cur))


def diagnosis_problem(run_dir: str, entry: dict) -> Optional[str]:
    """For a stall diagnosis with `decision: declare` (scripts/stall_rule.py):
    None when it cites a passing decision test of its finding or is exempt, else
    the problem. Its `finding:` is the identity the gate printed (kind:key)."""
    name = str(entry.get("finding") or "").strip()
    kind, _, key = name.partition(":")
    v = {"kind": kind, "key": key} if key else {"kind": kind}
    try:
        if kind_exempt(kind, load_policy()):
            return None
    except ValueError as e:
        return f"the decision-test policy cannot be read ({e})"
    status, detail, _res = entry_status(run_dir, entry, v)
    if status in ("tested", "exempt"):
        return None
    if status == "changes":
        return (f"its decision test (`{detail}`) finds the decision changes, so the finding cannot be declared: "
                "use `decision: fix` with the change")
    if status == "invalid":
        return detail
    return ("`decision: declare` needs `decision_test: <result file>` showing the decision does not change "
            "(the scope-declaration format, Phase 49 α), or `specified_by` + `quote` / `owner_amendment`")


# --------------------------------------------------------------------------- self-test

def _self_test() -> int:
    import json
    import tempfile
    fails = []

    def ok(c, m):
        print(("  ok   " if c else "  FAIL ") + m)
        if not c:
            fails.append(m)

    def w(d, rel, obj):
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(obj if isinstance(obj, str) else yaml.safe_dump(obj, sort_keys=False))

    def kinds(fs):
        return sorted(f["kind"] for f in fs)

    alloc = {"kind": "allocation_unstable", "severity": "HIGH", "stage": "OPTIMIZATION", "key": "allocation_robustness"}
    b15 = {"kind": "benchmark_drift", "severity": "HIGH", "stage": "MODEL", "key": "B15"}
    b19 = dict(b15, key="B19")
    wip = {"kind": "weakly_identified_parameters_persistent", "severity": "HIGH", "stage": "GATE"}
    wip_base = {"kind": "weakly_identified_parameters", "severity": "MEDIUM", "stage": "IDENTIFIABILITY"}
    test = {"finding": "allocation_unstable:allocation_robustness", "varied": "held-out fold at its own fit",
            "criterion": "package changes in >10% of units", "decision_changes": False,
            "recommendation": {"with": "plan A", "without": "plan A"}}
    pol = load_policy()
    with tempfile.TemporaryDirectory() as d:
        json.dump({"question": "Allocate the budget across districts for the program in 2026."},
                  open(os.path.join(d, "metadata.json"), "w"))
        ok(gate_findings(d, 5, [alloc, b15]) == [], "DT1 nothing declared: no finding before the post-WRITE gate")
        ok(kinds(gate_findings(d, 5, [alloc], post_write=True)) == [UNTESTED],
           "DT2 post-WRITE: an open testable HIGH no declaration covers is untested")
        sdecl = {"declarations": [
            {"id": "SD-002", "blocker_id": "benchmark_drift", "why_unresolved": "no data"},
            {"id": "SD-007", "blocker_id": "allocation_unstable", "why_unresolved": "no data"},
            {"id": "SD-008", "blocker_ids": ["weakly_identified_parameters",
                                             "weakly_identified_parameters_persistent"], "why_unresolved": "x"}]}
        w(d, "scope_declaration.yaml", sdecl)
        f = gate_findings(d, 8, [alloc, b15, b19, wip, wip_base], post_write=True)
        ok(kinds(f) == [UNTESTED] * 4 and {x["key"] for x in f} == {
            "allocation_unstable:allocation_robustness", "benchmark_drift:B15", "benchmark_drift:B19",
            "weakly_identified_parameters_persistent"},
           f"DT3 attempt 8's shape: SD-002 (B15, B19), SD-007, SD-008 untested: {[x['key'] for x in f]}")
        ok(all(x["scope_declarable"] is False for x in f), "DT4 none are scope-declarable")
        w(d, "models/decision_tests/alloc.yaml", test)
        sdecl["declarations"][1]["decision_test"] = "models/decision_tests/alloc.yaml"
        w(d, "scope_declaration.yaml", sdecl)
        ok(gate_findings(d, 8, [alloc]) == [], "DT5 a passing test bound to the finding lets the declaration stand")
        w(d, "models/decision_tests/alloc.yaml", dict(test, decision_changes=True))
        f = gate_findings(d, 8, [alloc])
        ok(kinds(f) == [CHANGES] and f[0]["declaration_test"]["entry"] == "SD-007",
           "DT6 decision_changes: true -> declared_finding_changes_decision")
        w(d, "models/decision_tests/alloc.yaml", dict(test, finding="benchmark_drift:B15"))
        ok(kinds(gate_findings(d, 8, [alloc])) == [INVALID], "DT7 a test of another finding does not count")
        w(d, "models/decision_tests/alloc.yaml", "finding: [unclosed\n")
        ok(kinds(gate_findings(d, 8, [alloc])) == [INVALID], "DT8 an unparseable result fails closed")
        w(d, "models/decision_tests/alloc.yaml", {k: v for k, v in test.items() if k != "recommendation"})
        ok(kinds(gate_findings(d, 8, [alloc])) == [INVALID], "DT9 no with/without recommendation: invalid")
        w(d, "models/decision_tests/alloc.yaml", dict(test, share_units_changed=0.3, share_threshold=0.1))
        ok(kinds(gate_findings(d, 8, [alloc])) == [INVALID], "DT10 share numbers contradicting the verdict: invalid")
        w(d, "models/decision_tests/alloc.yaml", dict(test, produced_by="harness"))
        ok(gate_findings(d, 8, [alloc]) == [], "DT10b produced_by: harness is accepted")
        w(d, "models/decision_tests/alloc.yaml", dict(test, produced_by="critic"))
        ok(kinds(gate_findings(d, 8, [alloc])) == [INVALID], "DT10c an unknown producer: invalid")
        sdecl["declarations"][1]["decision_test"] = "models/decision_tests/nope.yaml"
        w(d, "scope_declaration.yaml", sdecl)
        ok(kinds(gate_findings(d, 8, [alloc])) == [INVALID], "DT11 a missing result file: invalid")
        sdecl["declarations"][1]["decision_test"] = ["a.yaml", "b.yaml"]
        w(d, "scope_declaration.yaml", sdecl)
        ok(kinds(gate_findings(d, 8, [alloc])) == [INVALID], "DT12 one path, not a list")
        # B15 tested, B19 not: a kind-level entry needs one test per keyed instance.
        w(d, "models/decision_tests/b15.yaml", dict(test, finding="B15"))
        sdecl["declarations"][0]["decision_test"] = "models/decision_tests/b15.yaml"
        w(d, "scope_declaration.yaml", sdecl)
        f = gate_findings(d, 8, [b15, b19])
        ok(kinds(f) == [INVALID] and f[0]["key"] == "benchmark_drift:B19",
           f"DT13 B15's test does not cover B19 (one test per benchmark): {[(x['kind'], x['key']) for x in f]}")
        w(d, "models/decision_tests/b15.yaml", dict(test, finding="benchmark_drift"))
        ok(kinds(gate_findings(d, 8, [b15])) == [INVALID], "DT14 a keyed finding needs its key in `finding:`")
        # Exemptions.
        w(d, "decision_brief.yaml", {"question": "q", "decision": "which package per district",
                                     "out_of_scope": [{"id": "OS1", "text": "vector control is not in this grant"}]})
        e = {"id": "SD-9", "blocker_id": "benchmark_drift:B19", "why_unresolved": "x", "specified_by": "brief",
             "quote": "vector control is not in this grant"}
        sdecl["declarations"] = [e]
        w(d, "scope_declaration.yaml", sdecl)
        ok(gate_findings(d, 8, [b19]) == [], "DT15 specified_by brief with a verbatim quote: exempt")
        ok(exemption(d, e) == "specified by the brief: 'vector control is not in this grant'",
           f"DT15b exemption() gives the verified quote: {exemption(d, e)}")
        sdecl["declarations"] = [dict(e, quote="vector control is out of scope")]
        w(d, "scope_declaration.yaml", sdecl)
        ok(kinds(gate_findings(d, 8, [b19])) == [INVALID], "DT16 a quote not in the brief fails closed")
        w(d, "brief_amendments.yaml", {"amendments": [{"id": "AM-1", "item": "OS1", "change": "accept B19 as a known "
                                                       "drift", "reason": "the owner accepts this drift for the plan"}]})
        sdecl["declarations"] = [{"id": "SD-9", "blocker_id": "B19", "why_unresolved": "x", "owner_amendment": "AM-1"}]
        w(d, "scope_declaration.yaml", sdecl)
        ok(gate_findings(d, 8, [b19]) == [], "DT17 an owner amendment naming the finding: exempt")
        ok(exemption(d, sdecl["declarations"][0]) == "owner amendment AM-1"
           and exemption(d, {"id": "x", "blocker_id": "B19", "owner_amendment": "AM-2"}) is None
           and exemption(d, {"id": "x", "blocker_id": "B19"}) is None,
           "DT17b exemption(): the reason the gate accepts, else None")
        sdecl["declarations"] = [{"id": "SD-9", "blocker_id": "B19", "why_unresolved": "x", "owner_amendment": "AM-2"}]
        w(d, "scope_declaration.yaml", sdecl)
        ok(kinds(gate_findings(d, 8, [b19])) == [INVALID], "DT18 an unknown amendment fails closed")
        sdecl["declarations"] = [{"id": "SD-9", "blocker_id": "B19", "why_unresolved": "x"}]
        w(d, "scope_declaration.yaml", sdecl)
        ok(gate_findings(d, 8, [dict(b19, owner_signoff=True)], post_write=True) == []
           and gate_findings(d, 8, [dict(b19, key="B20", owner_signoff=True)], post_write=True) == [],
           "DT18b a finding put to the owner is not 'untested' here (declared or not)")
        # Out of scope: critic-derived, exempt classes, non-declarable, MEDIUM, accept blockers.
        sdecl["declarations"] = [{"id": "SD-1", "blockers": ["D-014_persistent", "done_when_unmet",
                                  "claims_ledger_missing", "coherence_audit_label_coherence", "figure_staleness_detected",
                                  "effort_floors_many_shortcuts", "plan_minimum_bar_failed", "required_artifact_missing",
                                  "x_invalid"], "why_unresolved": "x"}]
        w(d, "scope_declaration.yaml", sdecl)
        others = [{"kind": "D-014_persistent", "severity": "HIGH", "stage": "GATE"},
                  {"kind": "done_when_unmet", "severity": "HIGH", "stage": "CRITIQUE"},
                  {"kind": "claims_ledger_missing", "severity": "HIGH", "stage": "ANALYZE"},
                  {"kind": "coherence_audit_label_coherence", "severity": "HIGH", "stage": "WRITE"},
                  {"kind": "figure_staleness_detected", "severity": "HIGH", "stage": "ANALYZE"},
                  {"kind": "effort_floors_many_shortcuts", "severity": "HIGH", "stage": "MODEL"},
                  {"kind": "plan_minimum_bar_failed", "severity": "MEDIUM", "stage": "PLAN_CRITERIA"},
                  {"kind": "required_artifact_missing", "severity": "HIGH", "stage": "MODEL", "accept_blocker": True},
                  {"kind": "x_invalid", "severity": "HIGH", "stage": "MODEL", "scope_declarable": False}]
        ok(gate_findings(d, 8, others, post_write=True) == [], "DT19 exempt classes and critic findings need no test")
        ok(needs_test({"kind": "figure_validator_missing_persistent", "severity": "HIGH", "stage": "GATE"}, [], pol,
                      lambda k, e: False) is False, "DT20 a persistent escalation of an exempt kind is exempt")
        ok(needs_test({"kind": "sanity_check_failed_x_persistent", "severity": "HIGH", "stage": "GATE"},
                      [{"kind": "sanity_check_failed_x", "severity": "MEDIUM", "stage": "MODEL"}], pol,
                      lambda k, e: False), "DT21 a persistent escalation is judged by its base finding's stage")
        # DT30 (Codex P1): a parameter-set key is A17's identity (n=<size>:<hash>), and a
        # persistent escalation carries its base finding's instance identity.
        from lib.siblings import load_private
        fk = load_private("stall_rule", "_ma_p49_").finding_key
        unid = {"kind": "unidentified_parameters", "severity": "HIGH", "stage": "IDENTIFIABILITY", "key": ["b", "a"]}
        ident = f"unidentified_parameters:{fk(unid)}"
        w(d, "models/decision_tests/unid.yaml", dict(test, finding=ident))
        sdecl["declarations"] = [{"id": "SD-5", "blocker_id": "unidentified_parameters", "why_unresolved": "x",
                                  "decision_test": "models/decision_tests/unid.yaml"}]
        w(d, "scope_declaration.yaml", sdecl)
        ok(gate_findings(d, 8, [unid]) == [], "DT30 a test naming A17's identity of a parameter set counts")
        wkp = {"kind": "weakly_identified_parameters_persistent", "severity": "HIGH", "stage": "GATE"}
        wkb = {"kind": "weakly_identified_parameters", "severity": "MEDIUM", "stage": "IDENTIFIABILITY", "key": ["a", "b"]}
        w(d, "models/decision_tests/unid.yaml", dict(test, finding=f"weakly_identified_parameters:{fk(wkb)}"))
        sdecl["declarations"] = [{"id": "SD-8", "blocker_id": "weakly_identified_parameters", "why_unresolved": "x",
                                  "decision_test": "models/decision_tests/unid.yaml"}]
        w(d, "scope_declaration.yaml", sdecl)
        f = gate_findings(d, 8, [wkp, wkb])
        ok(f == [], f"DT31 a persistent escalation is matched by its base finding's instance: {f}")
        w(d, "models/decision_tests/unid.yaml", dict(test, finding="weakly_identified_parameters:n=9:00000000"))
        ok(kinds(gate_findings(d, 8, [wkp, wkb])) == [INVALID], "DT32 ...and a test of another parameter set does not")
        # DT33 (Codex round 2): a model-result failure is not an artifact gap; it needs a test.
        ok(needs_test({"kind": "compute_results_incomplete", "severity": "HIGH", "stage": "MODEL"}, [], pol,
                      lambda k, e: False)
           and needs_test({"kind": "benchmark_computed_missing", "severity": "HIGH", "stage": "MODEL"}, [], pol,
                          lambda k, e: False)
           and not needs_test({"kind": "claims_ledger_missing", "severity": "HIGH", "stage": "ANALYZE"}, [], pol,
                              lambda k, e: False),
           "DT33 failed model results need a decision test; a missing artifact does not")
        ok(all(isinstance(r, str) and r.strip() for r in pol["exempt_kinds"].values()) and "exempt_suffixes" not in pol,
           "DT34 the exemptions are named kinds, each with a reason (no suffix classes)")
        # DT35 (Codex round 2): a persistent escalation over several keyed base instances is covered
        # when each instance is.
        c1 = {"kind": "plan_minimum_bar_failed", "severity": "MEDIUM", "stage": "PLAN_CRITERIA", "key": "C1"}
        c2 = dict(c1, key="C2")
        pp = {"kind": "plan_minimum_bar_failed_persistent", "severity": "HIGH", "stage": "GATE"}
        w(d, "models/decision_tests/c1.yaml", dict(test, finding="plan_minimum_bar_failed:C1"))
        w(d, "models/decision_tests/c2.yaml", dict(test, finding="plan_minimum_bar_failed:C2"))
        sdecl["declarations"] = [
            {"id": "SD-C1", "blocker_id": "plan_minimum_bar_failed:C1", "why_unresolved": "x",
             "decision_test": "models/decision_tests/c1.yaml"},
            {"id": "SD-C2", "blocker_id": "plan_minimum_bar_failed:C2", "why_unresolved": "x",
             "decision_test": "models/decision_tests/c2.yaml"}]
        w(d, "scope_declaration.yaml", sdecl)
        f = gate_findings(d, 8, [pp, c1, c2])
        ok(f == [], f"DT35 each base instance tested: the escalation is covered: {[(x['kind'], x['key']) for x in f]}")
        sdecl["declarations"] = sdecl["declarations"][:1]
        w(d, "scope_declaration.yaml", sdecl)
        f = gate_findings(d, 8, [pp, c1, c2], post_write=True)
        ok([(x["kind"], x["key"]) for x in f] == [(UNTESTED, "plan_minimum_bar_failed_persistent:C2")],
           f"DT35b ...one instance untested: that instance is reported: {[(x['kind'], x['key']) for x in f]}")
        # Exposed data (for the report-citation check).
        w(d, "models/decision_tests/alloc.yaml", test)
        sdecl["declarations"] = [{"id": "SD-007", "blocker_id": "allocation_unstable", "why_unresolved": "x",
                                  "decision_test": "models/decision_tests/alloc.yaml"}]
        w(d, "scope_declaration.yaml", sdecl)
        t = tested_declarations(d)
        ok(len(t) == 1 and t[0]["entry_id"] == "SD-007" and t[0]["decision_changes"] is False
           and t[0]["recommendation"]["with"] == "plan A", f"DT22 tested_declarations: {t}")
        res, err = declaration_test(d, sdecl["declarations"][0])
        ok(res is not None and err is None and declaration_test(d, {"id": "x"}) == (None, None),
           "DT23 declaration_test loader: (result, None) / (None, None) without the field")
        # Stall diagnoses with decision: declare.
        diag = {"finding": "allocation_unstable:allocation_robustness", "decision": "declare"}
        ok("decision_test" in (diagnosis_problem(d, diag) or ""), "DT24 a declare diagnosis without a test")
        ok(diagnosis_problem(d, dict(diag, decision_test="models/decision_tests/alloc.yaml")) is None,
           "DT25 a declare diagnosis with a passing test")
        w(d, "models/decision_tests/alloc.yaml", dict(test, decision_changes=True))
        ok("decision: fix" in (diagnosis_problem(d, dict(diag, decision_test="models/decision_tests/alloc.yaml"))
                               or ""), "DT26 a declare diagnosis whose test changes the decision")
        ok(diagnosis_problem(d, {"finding": "claims_ledger_missing", "decision": "declare"}) is None,
           "DT27 an exempt kind needs no test in a diagnosis")
        # Fail closed on an unreadable declaration file and on a bad policy.
        w(d, "scope_declaration.yaml", "declarations: [unclosed\n")
        ok(gate_findings(d, 8, [alloc], post_write=True) == [],
           "DT28 an unreadable scope_declaration.yaml is left to scope_declaration_invalid (HIGH)")
        bad = os.path.join(d, "pol.yaml")
        w(d, "pol.yaml", {"decision_test": {"exempt_stages": "WRITE"}})
        try:
            load_policy(bad)
            ok(False, "DT29 a malformed policy raises")
        except ValueError:
            ok(True, "DT29 a malformed policy raises (the gate guard makes it HIGH)")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def _show(run_dir: str, round_n: Optional[int] = None) -> list[dict]:
    """CLI: the declarations and what each covers (no current findings: the
    gate supplies those). Prints the tested ones."""
    for t in tested_declarations(run_dir):
        print(f"{t['entry_id']}: {t['finding']} tested by {t[FIELD]}: decision_changes={t['decision_changes']}")
    return []


def main(argv=None) -> int:
    from lib import gate_cli
    return gate_cli.run(argv, __doc__, _show, _self_test, round_default=None)


if __name__ == "__main__":
    sys.exit(main())
