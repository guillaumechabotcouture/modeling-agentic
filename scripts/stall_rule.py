#!/usr/bin/env python3
"""A17 (Phase 47 ε): a gate finding that has not moved for N rounds needs a diagnosis.

    python3 scripts/stall_rule.py <run_dir> [--all] [--by-kind]   # per round, from validator_history.jsonl
    python3 scripts/stall_rule.py --self-test

E7 attempt 8 reported the same gate findings from round 5 to round 8 (three
benchmark drifts, the out-of-sample allocation instability, 19 weakly
identified transmission parameters, a failed minimum bar on the same
allocation metric, a sensitive sensitivity analysis, a failed sanity ratio)
while rounds 6-8 patched other things. Attempt 7 carried an unstable
sensitivity result and a benchmark drift from round 1 to round 8. The
persistence escalation (`<kind>_persistent`, Phase 12 β) raises a MEDIUM to
HIGH after a few rounds, but a HIGH asks for the same patch again. This rule
changes what the next round must produce instead: a diagnosis, or a scope
declaration.

Finding identity
----------------
`kind:key`. The key is set by the check that produces the finding, which
knows the instance: a `key` field (a benchmark id, a criterion id, a figure
or script path, a check name, the artifact a single verdict comes from; a
list, such as a parameter set, is recorded as its size and a short hash, so
a changed set is a new identity), or `artifact` for a missing required
artifact. A finding without a key is not tracked: its kind may have several
instances, and folding them into one would make a new instance look old.
The gate records each tracked finding's key in validator_history.jsonl.
History written before keys existed is not evidence: a finding's count
starts at the first gate that recorded keys.

Stalled
-------
Tracked: keyed MEDIUM and HIGH gate findings, except findings about the
write-up's own text (stage WRITE: the writer fixes them, there is no cause to
diagnose), the gate's and critics' bookkeeping (stages GATE, CRITIQUE), this
rule's own kinds, persistence escalations (`*_persistent`; their base finding
is tracked), records of something already resolved (`*_resolved`),
critic-derived kinds (`done_when_*`, `critique_*`), the scope-declaration
budget bookkeeping (`scope_declaration_*`) and `validator_check_crashed`.

A finding is stalled when it is present at this gate and at the last in-round
gate of each of the previous N-1 rounds (N = `stall_rounds` in
.claude/orchestration/effort_floors.yaml `defaults`), and no diagnosis entry
for it was recorded in those N rounds. Consecutive: a finding that cleared
for a round and came back starts again at 1. A fix that moved it was working
on it, and a regression of a fixed critic finding is `done_when_regressed`'s
job; a finding that flickers is still counted, by distinct rounds, by the
persistence escalation. A round with no gate record breaks the run of rounds.

A history line that cannot be read (not JSON, or the wrong shape) is HIGH
`stall_history_invalid`, not scope-declarable: a lost record must not read
as a round in which the finding cleared.

What the gate requires
----------------------
For each stalled finding, one of:
  * an entry in `models/stall_diagnoses.yaml` (manifest id `stall_diagnoses`)
    recorded within the last N rounds:

        diagnoses:
          - finding: "benchmark_drift:B15"      # as printed by the finding
            round: 6                             # the round it was written
            checks:                              # in this order, each class once
              - cause_class: missing_mechanism
                test: models/diag_b15_interaction.py   # an existing run file
                result: "the two modes combine multiplicatively; ..."
            decision: fix                        # fix | declare
            change: "add the shared-pathway term to ..."   # required for fix

    Cause classes, in the plan's B4 order: missing_data, missing_mechanism,
    structural_constraint, inconsistent_targets, mismatched_target_definition,
    bounds. An entry whose test file is missing, or that is otherwise
    malformed, does not count and is itself HIGH `stall_diagnoses_invalid`
    (fail closed; not scope-declarable).
  * a scope declaration naming its kind, its `kind:key` or its key in
    scope_declaration.yaml (only for a finding that is scope-declarable), with
    a passing decision test or an exemption (Phase 49 α, whatever the
    finding's severity; scripts/declaration_tests.py `declaration_counts`).

Without either: HIGH `finding_stalled_without_diagnosis` (target MODEL). It is
closed by the diagnosis or the declaration of the underlying finding; naming
this kind itself in scope_declaration.yaml does nothing, and it never lets the
early DECLARE_SCOPE (rule 3a) fire. At round exhaustion it is as declarable as
its underlying finding, so it cannot by itself turn DECLARE_SCOPE into
RUN_FAILED. A diagnosis with `decision: declare` satisfies this rule only; an
open HIGH underlying finding still needs its scope declaration. Phase 49 α
(A20): a `decision: declare` entry also needs `decision_test: <result file>`
showing the decision does not change, or the same exemptions as a scope
declaration (scripts/declaration_tests.py `diagnosis_problem`).

The CLI replays a run's history. `--by-kind` matches history written before
keys by kind alone (each kind one finding), an approximation for retro checks
of older runs only; the gate never does this.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Callable, Iterable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from lib.rigor_artifacts import artifact_path  # noqa: E402
from lib.safe_io import safe_load_mapping  # noqa: E402

STALL_KIND = "finding_stalled_without_diagnosis"
INVALID_KIND = "stall_diagnoses_invalid"
HISTORY_INVALID_KIND = "stall_history_invalid"
# The plan's B4 order (the calibration spec's DIAGNOSIS_ORDER), then the bounds.
CAUSE_CLASSES = ("missing_data", "missing_mechanism", "structural_constraint",
                 "inconsistent_targets", "mismatched_target_definition", "bounds")
DECISIONS = ("fix", "declare")
HISTORY_FILE = "validator_history.jsonl"
_FLOORS = os.path.join(os.path.dirname(HERE), ".claude", "orchestration", "effort_floors.yaml")
_DEFAULT_ROUNDS = 3
_UNTRACKED_PREFIXES = ("done_when_", "critique_", "scope_declaration_", "stall_diagnoses_", "stall_history_")
_UNTRACKED_KINDS = frozenset({STALL_KIND, "validator_check_crashed"})
# The write-up's own consistency (fixed by the writer, not diagnosed) and the gate's bookkeeping.
_UNTRACKED_STAGES = frozenset({"WRITE", "GATE", "CRITIQUE"})
_KEY_FIELDS = ("key", "artifact")


def stall_rounds(path: str = _FLOORS) -> int:
    """N from effort_floors.yaml `defaults.stall_rounds` (3 when absent).
    A value that is not an integer >= 2 raises (the gate's guard makes it HIGH)."""
    import yaml
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return _DEFAULT_ROUNDS
    n = (doc.get("defaults") or {}).get("stall_rounds", _DEFAULT_ROUNDS)
    if not isinstance(n, int) or isinstance(n, bool) or n < 2:
        raise ValueError(f"effort_floors.yaml defaults.stall_rounds must be an integer >= 2, got {n!r}")
    return n


def kind_tracked(kind: str, severity="MEDIUM", stage=None) -> bool:
    kind = str(kind or "")
    return (bool(kind) and severity in ("HIGH", "MEDIUM")
            and str(stage or "").upper() not in _UNTRACKED_STAGES
            and kind not in _UNTRACKED_KINDS and not kind.endswith(("_persistent", "_resolved"))
            and not kind.startswith(_UNTRACKED_PREFIXES))


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


def tracked(v: dict) -> bool:
    return kind_tracked(v.get("kind"), v.get("severity"), v.get("stage")) and bool(finding_key(v))


def identity(kind: str, key: Optional[str]) -> str:
    return f"{kind}:{key}" if key else str(kind)


def load_history(run_dir: str) -> tuple[dict[int, dict], list[str]]:
    """({round: {"ids", "legacy", "stage", "declared"}}, problems) from the last
    in-round gate of each round. `ids` are keyed observations; `legacy` the
    kinds of findings recorded without a key (pre-key history, or untracked).
    `problems` lists unreadable lines (fail closed: see HISTORY_INVALID_KIND)."""
    out: dict[int, dict] = {}
    path = os.path.join(run_dir, HISTORY_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return out, []
    except (OSError, UnicodeDecodeError) as e:
        return out, [f"the file cannot be read ({type(e).__name__})"]
    problems = []
    for no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            problems.append(f"line {no} is not JSON")
            continue
        rnd = rec.get("round") if isinstance(rec, dict) else None
        fs = rec.get("findings", []) if isinstance(rec, dict) else None
        if not isinstance(rnd, int) or isinstance(rnd, bool) or not isinstance(fs, list) \
                or not all(isinstance(x, dict) for x in fs):
            problems.append(f"line {no} is not a gate record (round, findings)")
            continue
        if rec.get("post_write"):
            continue
        ids, legacy, stage = set(), set(), {}
        for x in fs:
            if not x.get("kind"):
                continue
            ident = identity(str(x["kind"]), x.get("key"))
            (ids if x.get("key") else legacy).add(ident)
            stage[ident] = x.get("stage")
        out[rnd] = {"ids": ids, "legacy": legacy, "stage": stage,   # later records of a round replace earlier ones
                    "declared": {str(x) for x in rec.get("declared") or []}}
    return out, problems


def streak(hist: dict[int, dict], ident: str, round_n: int, by_kind: bool = False) -> list[int]:
    """Rounds, ending at round_n, in which the finding was observed without a gap.
    Only keyed observations count, unless by_kind (retro approximation)."""
    rounds = [round_n]
    r = round_n - 1
    while r in hist and (ident in hist[r]["ids"] or (by_kind and ident in hist[r]["legacy"])):
        rounds.append(r)
        r -= 1
    return sorted(rounds)


def _inside(run_dir: str, rel: str) -> Optional[str]:
    root = os.path.realpath(run_dir)
    p = os.path.realpath(os.path.join(run_dir, rel))
    return p if p == root or p.startswith(root + os.sep) else None


def load_diagnoses(run_dir: str, round_n: int) -> tuple[dict[str, list[int]], list[dict]]:
    """({finding identity: [rounds of valid entries]}, findings for invalid entries)."""
    path = artifact_path("stall_diagnoses", run_dir)
    shown = os.path.relpath(path, run_dir)
    doc, out = safe_load_mapping(path, "stall_diagnoses", stage="MODEL", display_path=shown)
    if doc is None:
        return {}, out
    entries = doc.get("diagnoses")
    if not isinstance(entries, list):
        return {}, [_invalid(f"`{shown}` needs a top-level `diagnoses:` list.")]
    valid: dict[str, list[int]] = {}
    for i, e in enumerate(entries):
        problems = _entry_problems(run_dir, e, round_n)
        name = e.get("finding") if isinstance(e, dict) else None
        if problems:
            out.append(_invalid(f"`{shown}` diagnoses[{i}] ({name or 'no finding'}): {'; '.join(problems)}. "
                                "It does not count as a diagnosis until fixed."))
        else:
            valid.setdefault(str(name).strip(), []).append(e["round"])
    return valid, out


def _entry_problems(run_dir: str, e, round_n: int) -> list[str]:
    if not isinstance(e, dict):
        return ["not a mapping"]
    p = []
    if not (isinstance(e.get("finding"), str) and e["finding"].strip()):
        p.append("`finding:` (the identity the gate printed) is missing")
    r = e.get("round")
    if not isinstance(r, int) or isinstance(r, bool) or r < 1 or r > round_n:
        p.append(f"`round:` must be the round it was written (1..{round_n}), got {r!r}")
    checks = e.get("checks")
    if not isinstance(checks, list) or not checks:
        p.append("`checks:` must list at least one {cause_class, test, result}")
        checks = []
    last = -1
    for j, c in enumerate(checks):
        if not isinstance(c, dict):
            p.append(f"checks[{j}] is not a mapping")
            continue
        cls = c.get("cause_class")
        if cls not in CAUSE_CLASSES:
            p.append(f"checks[{j}].cause_class {cls!r} is not one of {', '.join(CAUSE_CLASSES)}")
        elif CAUSE_CLASSES.index(cls) <= last:
            p.append(f"checks[{j}].cause_class {cls} is out of order (order: {', '.join(CAUSE_CLASSES)})")
        else:
            last = CAUSE_CLASSES.index(cls)
        test = c.get("test")
        if not (isinstance(test, str) and test.strip()):
            p.append(f"checks[{j}].test (the file the check ran or wrote) is missing")
        else:
            full = _inside(run_dir, test.strip().split("::")[0])
            if full is None:
                p.append(f"checks[{j}].test `{test}` is outside the run directory")
            elif not os.path.isfile(full) or os.path.getsize(full) == 0:
                p.append(f"checks[{j}].test `{test}` does not exist or is empty")
        if not (isinstance(c.get("result"), str) and c["result"].strip()):
            p.append(f"checks[{j}].result is missing")
    d = e.get("decision")
    if d not in DECISIONS:
        p.append(f"`decision:` must be one of {', '.join(DECISIONS)}, got {d!r}")
    elif d == "fix" and not (isinstance(e.get("change"), str) and e["change"].strip()):
        p.append("`decision: fix` needs `change:` naming the specific change")
    elif d == "declare":
        # Phase 49 α (A20): declaring a stalled finding needs the decision test a
        # scope declaration needs (or the same exemptions).
        from lib.siblings import load_private
        problem = load_private("declaration_tests", "_ma_p49_").diagnosis_problem(run_dir, e)
        if problem:
            p.append(problem)
    return p


def _invalid(claim: str, kind: str = INVALID_KIND, stage: str = "MODEL") -> dict:
    return {"kind": kind, "severity": "HIGH", "stage": stage, "target_stage": "MODEL",
            "scope_declarable": False, "claim": claim}


def _declaration_counts(run_dir: str, v: dict, current: list) -> bool:
    """Phase 49 α (Codex review): a declaration counts only with a passing decision
    test or an exemption (scripts/declaration_tests.py), whatever the severity."""
    from lib.siblings import load_private
    return load_private("declaration_tests", "_ma_p49_").declaration_counts(run_dir, v, current)


def gate_findings(run_dir: str, round_n: Optional[int] = None, current: Optional[list] = None,
                  declared: Iterable[str] = (),
                  non_declarable: Callable[[str, object], bool] = lambda k, e: e is False) -> list[dict]:
    """Stalled findings among `current` (this gate's findings so far), invalid
    diagnosis entries and unreadable history. Sets `stall_key` on each tracked
    finding of `current`, which the gate records in validator_history.jsonl."""
    current = [v for v in (current or []) if isinstance(v, dict)]
    for v in current:
        if tracked(v):
            v["stall_key"] = finding_key(v)
    if round_n is None:
        return []
    n = stall_rounds()
    diagnosed, out = load_diagnoses(run_dir, round_n)
    hist, problems = load_history(run_dir)
    if problems:
        out.append(_invalid(
            f"`{HISTORY_FILE}` has {len(problems)} unreadable record(s) ({'; '.join(problems[:5])}). The gate "
            "cannot tell whether a finding cleared in those rounds, so it does not assume so. The file is "
            "written by the validator; restore the damaged lines from the run's progress.md / stage-7 output, "
            "or remove them and record why in progress.md.", HISTORY_INVALID_KIND, "GATE"))
    declared = {str(x) for x in declared}
    seen: set[str] = set()
    for v in current:
        if "stall_key" not in v:
            continue
        kind, key = str(v["kind"]), v["stall_key"]
        ident = identity(kind, key)
        if ident in seen:
            continue
        seen.add(ident)
        rounds = streak(hist, ident, round_n)
        if len(rounds) < n:
            continue
        nd = bool(non_declarable(kind, v.get("scope_declarable")))
        if not nd and {kind, ident, key} & declared and _declaration_counts(run_dir, v, current):
            continue
        if any(r >= round_n - n + 1 for r in diagnosed.get(ident, [])):
            continue
        out.append({
            "kind": STALL_KIND, "severity": "HIGH", "stage": "GATE", "target_stage": "MODEL",
            "scope_declarable": not nd,
            "stall": {"finding": ident, "rounds": rounds, "severity": v.get("severity"), "declarable": not nd},
            "claim": (f"`{ident}` ({v.get('severity')}) has been at the gate for {len(rounds)} rounds in a row "
                      f"({rounds[0]}-{rounds[-1]}) with no diagnosis recorded in the last {n}: another patch "
                      f"is not the next step. Before the next gate the modeler records in "
                      f"`models/stall_diagnoses.yaml` an entry with finding: \"{ident}\", round, checks "
                      f"(cause_class in order {', '.join(CAUSE_CLASSES)}; test: an existing file; result), and "
                      f"decision: fix (with change:) or declare"
                      + ("" if nd else f", or the lead declares `{kind}` in scope_declaration.yaml")
                      + f". Finding: \"{str(v.get('claim') or '')[:160]}\""),
        })
    return out


def replay(run_dir: str, n: Optional[int] = None, honour_declared: bool = True,
           by_kind: bool = False) -> list[dict]:
    """For each round of validator_history.jsonl, the findings that would have
    been stalled at that round's last in-round gate. `declared` is what that
    gate recorded as declared; with honour_declared=False every stall is listed.
    by_kind also counts pre-key records, one finding per kind (an approximation
    for older runs). Diagnoses (which no past run wrote) are not considered."""
    n = n or stall_rounds()
    hist, _problems = load_history(run_dir)
    out, first = [], {}
    for rnd in sorted(hist):
        for ident in sorted(hist[rnd]["ids"] | (hist[rnd]["legacy"] if by_kind else set())):
            kind, _, key = ident.partition(":")
            if not kind_tracked(kind, "MEDIUM", hist[rnd]["stage"].get(ident)):
                continue
            past = {r: h for r, h in hist.items() if r < rnd}
            rounds = streak(past, ident, rnd, by_kind)
            if len(rounds) < n:
                continue
            declared = bool({kind, ident, key} & hist[rnd]["declared"])
            if declared and honour_declared:
                continue
            first.setdefault(ident, rnd)
            out.append({"round": rnd, "finding": ident, "rounds": rounds, "first_flagged": first[ident],
                        "declared": declared})
    return out


# --------------------------------------------------------------------------- self-test

def _self_test() -> int:
    import tempfile
    import yaml
    fails: list[str] = []

    def ok(c, m):
        if not c:
            fails.append(m)

    bd = lambda b: {"kind": "benchmark_drift", "severity": "HIGH", "stage": "MODEL", "key": b,  # noqa: E731
                    "claim": f"Benchmark `{b}` (x): observed 0.9 outside band [1, 2]"}
    mb = {"kind": "plan_minimum_bar_failed", "severity": "MEDIUM", "key": "MB-005",
          "claim": "Minimum bar MB-005 FAILED: worst-fold rank correlation >= 0.7 (0.35 → FAIL)"}
    fig = lambda name: {"kind": "figure_staleness_detected", "severity": "HIGH", "stage": "ANALYZE",  # noqa: E731
                        "key": f"figures/{name}", "claim": f"Figure {name} was drawn from source data that changed"}

    def hist(d, rounds: dict, keyed=True, post_write=()):
        """Records as the gate writes them: a key only on tracked findings."""
        with open(os.path.join(d, HISTORY_FILE), "w") as f:
            for r, vs in rounds.items():
                fs = [{"kind": v["kind"], "severity": v["severity"], "stage": v.get("stage"),
                       **({"key": finding_key(v)} if keyed and tracked(v) else {})} for v in vs]
                f.write(json.dumps({"round": r, "post_write": False, "findings": fs}) + "\n")
            for r in post_write:
                f.write(json.dumps({"round": r, "post_write": True, "findings": []}) + "\n")

    def diag(d, entries):
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        yaml.safe_dump({"diagnoses": entries}, open(os.path.join(d, "models", "stall_diagnoses.yaml"), "w"))

    def run(d, cur, rnd, **kw):
        return gate_findings(d, rnd, [dict(v) for v in cur], **kw)

    stalled = lambda fs: sorted(f["stall"]["finding"] for f in fs if f["kind"] == STALL_KIND)  # noqa: E731

    ok(stall_rounds() == 3, f"S0 N comes from effort_floors.yaml (3), got {stall_rounds()}")
    import importlib.util
    spec = importlib.util.spec_from_file_location("_stall_p47_calibration_spec", os.path.join(HERE, "calibration_spec.py"))
    cs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs)
    ok(CAUSE_CLASSES == tuple(cs.DIAGNOSIS_ORDER) + ("bounds",),
       "S1 the cause classes are the calibration spec's B4 order, then the bounds")
    ok(finding_key(bd("B9")) == "B9" and finding_key(mb) == "MB-005"
       and finding_key({"kind": "required_artifact_missing", "artifact": "pre_mortem"}) == "pre_mortem",
       "K1 the key is the field the producing check set")
    wk = lambda ps: {"kind": "weakly_identified_parameters", "severity": "MEDIUM", "key": ps,  # noqa: E731
                     "claim": f"flagged {len(ps)} weakly identified parameter(s): {ps!r}."}
    ok(finding_key(wk(["b", "a"])) == finding_key(wk(["a", "b"])) != finding_key(wk(["a", "c"]))
       and finding_key(wk(["a"])).startswith("n=1:"), "K3 a parameter set: order-free, a changed set differs")
    ok(not tracked({"kind": "figure_staleness_detected", "severity": "HIGH", "stage": "ANALYZE",
                    "claim": "Figure calibration.png was drawn from source data that changed"}),
       "K4 (Codex P1) a finding whose check set no key is not tracked, whatever its claim says")
    ok(finding_key({"kind": "x", "key": True}) == "" and finding_key({"kind": "x", "key": []}) == "",
       "K5 a key that is not a value is no key")

    with tempfile.TemporaryDirectory() as d:
        # T1: three rounds in a row, no diagnosis: HIGH.
        hist(d, {1: [bd("B9"), mb], 2: [bd("B9"), mb]})
        f = run(d, [bd("B9"), mb], 3)
        ok(stalled(f) == ["benchmark_drift:B9", "plan_minimum_bar_failed:MB-005"]
           and all(x["severity"] == "HIGH" and x["scope_declarable"] for x in f),
           f"T1 present 3 rounds without a diagnosis → HIGH, declarable like its finding: {f}")
        ok(stalled(run(d, [bd("B9")], 2)) == [], "T1b two rounds is not yet stalled")
        cur = [bd("B9")]
        gate_findings(d, 3, cur)
        ok(cur[0].get("stall_key") == "B9", "T1c the key is set on the finding for the history record")
        # T2: a valid diagnosis written within the window clears it.
        open(os.path.join(d, "models_test.txt"), "w").write("ran")
        good = {"finding": "benchmark_drift:B9", "round": 3, "decision": "fix", "change": "refit the PBO effect",
                "checks": [{"cause_class": "missing_data", "test": "models_test.txt", "result": "data present"},
                           {"cause_class": "inconsistent_targets", "test": "models_test.txt", "result": "stale"}]}
        diag(d, [good])
        f = run(d, [bd("B9"), mb], 3)
        ok(stalled(f) == ["plan_minimum_bar_failed:MB-005"] and len(f) == 1,
           f"T2 a valid diagnosis clears its finding only: {f}")
        # T2b: it lasts N rounds, then the finding stalls again.
        hist(d, {1: [bd("B9")], 2: [bd("B9")], 3: [bd("B9")], 4: [bd("B9")]})
        ok(stalled(run(d, [bd("B9")], 5)) == [], "T2b a round-3 diagnosis still counts at round 5")
        hist(d, {r: [bd("B9")] for r in range(1, 6)})
        ok(stalled(run(d, [bd("B9")], 6)) == ["benchmark_drift:B9"], "T2c ...and no longer at round 6")
        # T3: a diagnosis citing a missing test artifact fails closed.
        hist(d, {1: [bd("B9")], 2: [bd("B9")]})
        diag(d, [dict(good, checks=[{"cause_class": "missing_data", "test": "models/nope.py", "result": "x"}])])
        f = run(d, [bd("B9")], 3)
        ok(sorted(x["kind"] for x in f) == [STALL_KIND, INVALID_KIND]
           and all(x["severity"] == "HIGH" for x in f)
           and next(x for x in f if x["kind"] == INVALID_KIND)["scope_declarable"] is False,
           f"T3 a missing test artifact: the entry is HIGH invalid and the finding is still stalled: {f}")
        for bad, why in ((dict(good, checks=list(reversed(good["checks"]))), "out of order"),
                         (dict(good, decision="fix", change=""), "change"),
                         (dict(good, round=9), "round"),
                         (dict(good, checks=[{"cause_class": "luck", "test": "models_test.txt", "result": "x"}]),
                          "cause_class"),
                         (dict(good, checks=[{"cause_class": "bounds", "test": "../outside.txt", "result": "x"}]),
                          "outside")):
            diag(d, [bad])
            f = run(d, [bd("B9")], 3)
            ok(stalled(f) == ["benchmark_drift:B9"] and any(why in x["claim"] for x in f if x["kind"] == INVALID_KIND),
               f"T3b an entry with a bad {why} does not count and is reported: {f}")
        # T3d (Phase 49 α): `decision: declare` needs a decision test of its finding.
        declare = dict(good, decision="declare", change=None)
        diag(d, [declare])
        f = run(d, [bd("B9")], 3)
        ok(stalled(f) == ["benchmark_drift:B9"] and any("decision_test" in x["claim"] for x in f
                                                         if x["kind"] == INVALID_KIND),
           f"T3d a declare diagnosis without a decision test does not count: {f}")
        yaml.safe_dump({"finding": "B9", "varied": "PBO effect at the trial value", "criterion": "package changes",
                        "decision_changes": False, "recommendation": {"with": "A", "without": "A"}},
                       open(os.path.join(d, "models", "dt_b9.yaml"), "w"))
        diag(d, [dict(declare, decision_test="models/dt_b9.yaml")])
        ok(run(d, [bd("B9")], 3) == [], "T3e ...with a passing decision test it does")
        open(os.path.join(d, "models", "stall_diagnoses.yaml"), "w").write("diagnoses: [unclosed\n")
        ok([x["kind"] for x in run(d, [], 3)] == [INVALID_KIND], "T3c an unparseable file is HIGH invalid")
        os.remove(os.path.join(d, "models", "stall_diagnoses.yaml"))
        # T4: a changed key is a different finding.
        hist(d, {1: [bd("B9")], 2: [bd("B15")]})
        ok(stalled(run(d, [bd("B19")], 3)) == [], "T4 B9, B15, B19 in turn: nothing stalled")
        hist(d, {1: [wk(["a", "b"])], 2: [wk(["a", "b"])]})
        ok(stalled(run(d, [wk(["a", "b", "c"])], 3)) == [], "T4b a changed parameter set: not stalled")
        hist(d, {1: [fig("calibration.png")], 2: [fig("calibration.png")]})
        ok(stalled(run(d, [fig("allocation.png")], 3)) == [],
           "T4c (Codex P1) a new stale figure in round 3 is not the old one")
        ok(stalled(run(d, [fig("calibration.png")], 3)) == ["figure_staleness_detected:figures/calibration.png"],
           "T4d ...the same figure stale three rounds is")
        # T5: resolved for a round, then back: the count restarts.
        hist(d, {1: [bd("B9")], 2: [bd("B9")], 3: [], 4: [bd("B9")]})
        ok(stalled(run(d, [bd("B9")], 5)) == [], "T5 cleared at round 3: rounds 4-5 are a new run of 2")
        hist(d, {1: [bd("B9")], 2: [bd("B9")], 4: [bd("B9")]})
        ok(stalled(run(d, [bd("B9")], 5)) == [], "T5b a round with no gate record breaks the run")
        # T6: a scope declaration of the finding (kind, kind:key or key) clears it, when the
        # declaration has a decision test (Phase 49 α).
        hist(d, {1: [bd("B9"), mb], 2: [bd("B9"), mb]})
        yaml.safe_dump({"finding": "B9", "varied": "PBO effect at the trial value", "criterion": "package changes",
                        "decision_changes": False, "recommendation": {"with": "A", "without": "A"}},
                       open(os.path.join(d, "models", "dt_b9.yaml"), "w"))
        for decl in (["benchmark_drift"], ["benchmark_drift:B9"], ["B9"]):
            yaml.safe_dump({"declarations": [{"id": "SD-1", "blocker_id": decl[0], "why_unresolved": "x",
                                              "decision_test": "models/dt_b9.yaml"}]},
                           open(os.path.join(d, "scope_declaration.yaml"), "w"))
            ok(stalled(run(d, [bd("B9")], 3, declared=decl)) == [], f"T6 declared as {decl}")
        # T6d (Codex P1): a MEDIUM stalled finding declared with no decision test stays stalled.
        yaml.safe_dump({"declarations": [{"id": "SD-2", "blocker_id": "plan_minimum_bar_failed",
                                          "why_unresolved": "x"}]}, open(os.path.join(d, "scope_declaration.yaml"), "w"))
        ok(stalled(run(d, [mb], 3, declared=["plan_minimum_bar_failed"])) == ["plan_minimum_bar_failed:MB-005"],
           "T6d a declaration without a decision test does not clear a stalled MEDIUM")
        yaml.safe_dump({"declarations": [{"id": "SD-1", "blocker_id": "benchmark_drift", "why_unresolved": "x",
                                          "decision_test": "models/dt_b9.yaml"}]},
                       open(os.path.join(d, "scope_declaration.yaml"), "w"))
        ok(stalled(run(d, [bd("B9")], 3, declared=[STALL_KIND])) == ["benchmark_drift:B9"],
           "T6b declaring the stall kind itself does nothing")
        nd = run(d, [bd("B9")], 3, declared=["benchmark_drift"], non_declarable=lambda k, e: True)
        ok(stalled(nd) == ["benchmark_drift:B9"] and nd[0]["scope_declarable"] is False,
           "T6c a non-declarable finding needs the diagnosis; its stall finding is not declarable")
        # T7: pre-key history is no evidence; post-write records are ignored.
        hist(d, {1: [bd("B9")], 2: [bd("B15")]}, keyed=False)
        ok(stalled(run(d, [bd("B19")], 3)) == [], "T7 (Codex P1) kind-only history does not match B19")
        hist(d, {1: [bd("B9")], 2: [bd("B9")]}, keyed=False)
        ok(stalled(run(d, [bd("B9")], 3)) == [], "T7a ...nor B9: the count starts at the first keyed round")
        with open(os.path.join(d, HISTORY_FILE), "a") as f:
            for r in (3, 4):
                f.write(json.dumps({"round": r, "findings": [{"kind": "benchmark_drift", "severity": "HIGH",
                                                              "key": "B9"}]}) + "\n")
        ok(stalled(run(d, [bd("B9")], 5)) == ["benchmark_drift:B9"], "T7c keyed rounds 3-5 are three rounds")
        hist(d, {1: [bd("B9")], 2: [bd("B9")]}, post_write=[2])
        ok(stalled(run(d, [bd("B9")], 3)) == ["benchmark_drift:B9"], "T7b a post-write record does not erase a round")
        # T8: untracked kinds, LOW severity, and duplicates.
        low = {"kind": "scope_declaration_cap_raised", "severity": "LOW", "key": "x", "claim": "x"}
        per = {"kind": "weakly_identified_parameters_persistent", "severity": "HIGH", "key": "x", "claim": "x"}
        dw = {"kind": "done_when_unmet", "severity": "MEDIUM", "key": "P-012", "claim": "P-012 test fails"}
        hist(d, {1: [low, per, dw], 2: [low, per, dw]})
        ok(run(d, [low, per, dw], 3) == [], "T8 LOW, *_persistent and critic-derived kinds are not tracked")
        hist(d, {1: [bd("B9")] * 2, 2: [bd("B9")] * 2})
        ok(len(run(d, [bd("B9")] * 2, 3)) == 1, "T8b one stall finding per identity")
        ok(gate_findings(d, None, [bd("B9")]) == [], "T8c no round: silent")
        resolved = {"kind": "artifact_integrity_resolved", "severity": "MEDIUM", "key": "x", "claim": "regenerated"}
        txt = {"kind": "same_doc_inconsistency", "severity": "MEDIUM", "stage": "WRITE", "key": "x", "claim": "2"}
        hist(d, {1: [resolved, txt], 2: [resolved, txt]})
        ok(run(d, [resolved, txt], 3) == [], "T8d *_resolved and write-up findings are not tracked")
        # T9: replay reports the first round each finding would have stalled.
        hist(d, {1: [bd("B9"), mb], 2: [bd("B9"), mb], 3: [bd("B9")], 4: [bd("B9"), mb]}, keyed=False)
        ok(replay(d, 3) == [], "T9 replay of pre-key history finds nothing unless asked to match by kind")
        rp = replay(d, 3, by_kind=True)
        ok({(x["finding"], x["round"]) for x in rp} == {("benchmark_drift", 3), ("benchmark_drift", 4)},
           f"T9a replay by kind: {rp}")
        with open(os.path.join(d, HISTORY_FILE), "a") as f:
            f.write(json.dumps({"round": 4, "declared": ["benchmark_drift"],
                                "findings": [{"kind": "benchmark_drift", "severity": "HIGH"}]}) + "\n")
        ok([x["round"] for x in replay(d, 3, by_kind=True)] == [3]
           and len(replay(d, 3, honour_declared=False, by_kind=True)) == 2,
           "T9b replay honours what each gate recorded as declared")
        # T11 (Codex P1): unreadable history fails closed instead of reading as a cleared round.
        hist(d, {1: [bd("B9")], 2: [bd("B9")]})
        with open(os.path.join(d, HISTORY_FILE), "a") as f:
            f.write('{"round": 3, "findings": [{"kind": "bench\n')
        f = run(d, [bd("B9")], 4)
        ok([x["kind"] for x in f] == [HISTORY_INVALID_KIND] and f[0]["severity"] == "HIGH"
           and f[0]["scope_declarable"] is False and "line 3" in f[0]["claim"],
           f"T11 a truncated record is HIGH stall_history_invalid, not declarable: {f}")
        for bad in ('[1, 2]\n', '{"round": "3", "findings": []}\n', '{"round": 3, "findings": [1]}\n'):
            hist(d, {1: [bd("B9")]})
            open(os.path.join(d, HISTORY_FILE), "a").write(bad)
            ok([x["kind"] for x in run(d, [], 3)] == [HISTORY_INVALID_KIND], f"T11b a wrong-shape record: {bad!r}")
        hist(d, {1: [bd("B9")]})
        open(os.path.join(d, HISTORY_FILE), "a").write("\n" + json.dumps({"round": 2, "action": "X"}) + "\n")
        ok(run(d, [], 3) == [], "T11c blank lines and records without findings are fine")
    # T10: the threshold is read from the manifest and must be sane.
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "f.yaml")
        open(p, "w").write("defaults:\n  stall_rounds: 4\n")
        ok(stall_rounds(p) == 4, "T10 stall_rounds read from the manifest")
        open(p, "w").write("defaults:\n  stall_rounds: 1\n")
        try:
            stall_rounds(p)
            ok(False, "T10b stall_rounds 1 must raise")
        except ValueError:
            pass

    if fails:
        print(f"FAIL: {len(fails)} case(s)", file=sys.stderr)
        for m in fails:
            print(f"  - {m}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run_dir", nargs="?")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--all", action="store_true", help="also list stalls the run had declared")
    p.add_argument("--by-kind", action="store_true",
                   help="count pre-key history by kind (retro approximation for older runs)")
    a = p.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir or not os.path.isdir(a.run_dir):
        p.error("run_dir is required")
    _hist, problems = load_history(a.run_dir)
    for msg in problems:
        print(f"unreadable history: {msg}")
    for r in replay(a.run_dir, honour_declared=not a.all, by_kind=a.by_kind):
        print(f"round {r['round']}: {r['finding']} stalled (present rounds {r['rounds'][0]}-{r['rounds'][-1]}, "
              f"first flagged round {r['first_flagged']}){' [declared]' if r['declared'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
