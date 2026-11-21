#!/usr/bin/env python3
"""The conflict rule (W0-F, owner default D5, 2026-11-21): a calibration target the run cannot meet is held out,
declared and tested, and the run continues; it pauses for the owner only when the test changes the plan.

    python3 toolkit/analysis/conflicts.py --self-test

The signed spec states the policy (the signature covers it, so applying it is not an amendment):

    conflicts:
      policy: declare_and_test      # the default the harness writes into a new draft; "stop" keeps today's behaviour
      max_share_declared: 0.05      # optional: of each kind's targets (at least one may always be declared)
      never_declare: [<id>]         # optional: targets that must be met (a target, source or unit id, or an exact key)
      test_scales: [0.8, 1.25]      # optional: the held-out target's units' calibration targets at these scales

A spec without `conflicts` keeps today's behaviour (stop). Under declare_and_test, when the joint calibration is
blocked only by misses the existing declaration path understands (a unit no value matches, a group target the
verification fails, a shared offset whose region-sample rows fail, with its row breakdown), each miss nobody declared
is declared automatically (joint_calibration.unmatched_decision decides with those declarations added, so every
consistency check it makes still holds). Each gets two automatic decision tests, `conflict_<n>_<lo|hi>`: its units'
calibration targets scaled to each end of `test_scales`, their calibrated values redone, the plan re-solved
(decision_tests calibration_target). The run continues. analysis/conflicts.yaml records each conflict, why it was
declared, its tests and their verdicts; a conflict whose test changes the plan, or could not run, needs the owner.
The findings ledger carries each as a HIGH `evidence_conflict_declared` finding; the stage machine pauses once at
OWNER_DECISION for every conflict that needs the owner, batched. The report lists every held-out target.

Owner decision 2026-11-21 (W0-F review r1): no refit by default; the tests bound the miss. A declared miss is not
refitted away. The calibration's region sample
and best point are the ones the blocked fit produced (for an offset miss, its failed rows redrawn from the rows that
met it), as for an owner-declared miss today; the automatic tests bound how far the miss can move the plan. When a
test changes the plan, the owner's request lists "refit without the target" as an option, with its estimated cost
(refit_estimate: the joint calibration's own simulator calls); the harness does not run it.

Limits that stay hard (the run stops as today): a conflict on a never_declare target; more conflicts of a kind than
its share; any other block (no accepted region, an emulator check, an offset block without its row breakdown).
Nothing here names a disease.
"""
from __future__ import annotations

import copy
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PRODUCER = "toolkit/analysis/conflicts.py"
POLICIES = ("declare_and_test", "stop")
KEYS = {"policy", "max_share_declared", "never_declare", "test_scales"}
DEFAULTS = {"max_share_declared": 0.05, "test_scales": [0.8, 1.25]}
DECLARABLE = {"unmatched_units", "verification_failed", "offset_sampling_low_acceptance"}
TEST_PREFIX = "conflict_"
FINDING_KIND = "evidence_conflict_declared"
ARTIFACT = "analysis/conflicts.yaml"
AUTO_REASON = "declared under conflicts.policy declare_and_test: the evidence conflicts with the model or other targets"


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def spec_problems(raw: dict) -> list:
    c = raw.get("conflicts") if isinstance(raw, dict) else None
    if c is None:
        return []
    if not isinstance(c, dict):
        return ["conflicts: a mapping {policy, max_share_declared, never_declare, test_scales}"]
    bad = [f"conflicts: unknown keys {sorted(set(c) - KEYS)}"] if set(c) - KEYS else []
    if c.get("policy") not in POLICIES:
        bad.append(f"conflicts.policy: one of {list(POLICIES)}")
    s = c.get("max_share_declared", DEFAULTS["max_share_declared"])
    if not (_num(s) and 0 <= s <= 1):
        bad.append("conflicts.max_share_declared: a number in [0, 1]")
    nd = c.get("never_declare", [])
    if not (isinstance(nd, list) and all(isinstance(x, str) and x.strip() for x in nd)):
        bad.append("conflicts.never_declare: a list of target, source or unit ids (text)")
    an = raw.get("analysis") if isinstance(raw.get("analysis"), dict) else {}
    taken = [t.get("id") for t in an.get("decision_tests") or [] if isinstance(t, dict)
             and str(t.get("id")).startswith(TEST_PREFIX)]
    if taken and c.get("policy") == "declare_and_test":
        bad.append(f"analysis.decision_tests: ids {taken[:3]} start with {TEST_PREFIX!r}, which the conflict rule's "
                   "automatic tests use")
    ts = c.get("test_scales", DEFAULTS["test_scales"])
    if not (isinstance(ts, list) and len(ts) == 2 and all(_num(x) and x > 0 and x != 1 for x in ts) and ts[0] < ts[1]):
        bad.append("conflicts.test_scales: [low, high], positive, increasing, neither 1")
    return bad


def policy(raw: dict) -> dict:
    c = raw.get("conflicts") if isinstance(raw, dict) and isinstance(raw.get("conflicts"), dict) else {}
    return {"policy": c.get("policy", "stop"), "max_share_declared": float(c.get("max_share_declared",
                                                                                  DEFAULTS["max_share_declared"])),
            "never_declare": [str(x) for x in c.get("never_declare") or []],
            "test_scales": [float(x) for x in c.get("test_scales", DEFAULTS["test_scales"])]}


def ensure_default(run_dir: str, rel: str = "decision_spec.yaml") -> bool:
    """A new draft without `conflicts` gets the owner's default (D5) appended, before it is shown for signing. True
    when it was added. A draft that states a policy is left as written."""
    import yaml
    if os.path.join(ROOT, "scripts") not in sys.path:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
    from lib.safe_io import read_nofollow, write_nofollow
    data = read_nofollow(run_dir, rel)[0]
    raw = yaml.safe_load(data.decode("utf-8"))
    if not isinstance(raw, dict) or "conflicts" in raw:
        return False
    text = data.decode("utf-8").rstrip("\n") + "\n# owner default D5 (2026-11-21): declare, test and continue\n" \
        "conflicts:\n  policy: declare_and_test\n"
    try:
        ok_ = (yaml.safe_load(text) or {}).get("conflicts", {}).get("policy") == "declare_and_test"
    except (yaml.YAMLError, AttributeError):
        ok_ = False
    if not ok_:             # a draft the appended block does not extend (flow style, an end marker): left as written
        return False
    write_nofollow(run_dir, rel, text.encode("utf-8"), atomic=True)
    return True


def _identities(spec) -> dict:
    """{kind: {key: (target id, [unit ids])}} for every declarable identity the spec's joint targets generate."""
    from toolkit.analysis import joint_calibration as JC
    j = JC.declared(spec) or {}
    rows = list(spec.units())
    uid = spec.uid
    ut = [t for t in j.get("targets") or [] if isinstance(t, dict) and t.get("kind") == "unit"]
    out = {"group_target": {}, "offset": {}, "unit_targets": [str(t["id"]) for t in ut],
           # Codex r2 P1-1: a unit miss and an offset are protected by the unit targets' ids and their sources alike
           "unit_names": {str(t["id"]) for t in ut} | {str(t.get("bias_source")) for t in ut if t.get("bias_source")},
           "source_targets": {str(t.get("bias_source", t["id"])): str(t["id"]) for t in ut if t.get("bias_by")}}
    for t in j.get("targets") or []:
        if not isinstance(t, dict):
            continue
        if t.get("kind") == "group":
            import csv
            col = (t.get("key") or {}).get("units")
            with open(spec.resolve(t["csv"]), newline="", encoding="utf-8") as f:     # the groups it observes
                obs = {str(r.get((t.get("key") or {}).get("csv"))) for r in csv.DictReader(f)
                       if all(str(r.get(k)) == str(v) for k, v in (t.get("filter") or {}).items())}
            for r in rows:
                if col and r.get(col) is not None and str(r[col]) in obs:
                    out["group_target"].setdefault(f"{t['id']}:{r[col]}", (t["id"], []))[1].append(str(r[uid]))
        if t.get("kind") == "unit" and t.get("bias_by"):
            src = str(t.get("bias_source", t.get("id")))
            for r in rows:
                if r.get(t["bias_by"]) is not None:
                    k = f"{src}|{r[t['bias_by']]}"
                    us = out["offset"].setdefault(k, (src, []))[1]
                    if str(r[uid]) not in us:
                        us.append(str(r[uid]))
    return out


def _slug(key: str) -> str:
    import hashlib            # stable across rebuilds (an owner's answer binds the claim, which names the tests)
    return (re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_")[:32] or "x") + "_" + \
        hashlib.sha256(key.encode()).hexdigest()[:6]


def declare(spec, rep: dict, dec: dict) -> dict:
    """The calibration decision under the spec's conflict policy. Returns `dec` with `conflicts` added: {policy,
    declared: [{n, kind, key, target, units, why, tests}], refused}. Under declare_and_test, a blocked report whose
    misses can all be declared proceeds with them declared (each with its automatic tests); otherwise `dec` is
    returned unchanged (today's behaviour), with why in `refused`."""
    from toolkit.analysis import joint_calibration as JC
    pol = policy(spec.raw)
    out = dict(dec, conflicts={"policy": pol["policy"], "declared": [], "refused": None})
    blocks = rep.get("blocks") or []
    if pol["policy"] != "declare_and_test" or dec.get("proceed") or not (rep.get("blocked") or blocks):
        return out
    codes = {b.get("code") for b in blocks if isinstance(b, dict)}
    if not blocks or codes - DECLARABLE or not all(isinstance(b, dict) for b in blocks):
        out["conflicts"]["refused"] = f"blocks {sorted(map(str, codes - DECLARABLE))} cannot be declared (a hard limit)"
        return out
    j = JC.declared(spec) or {}
    u = j.get("unmatched") if isinstance(j.get("unmatched"), dict) else {}
    have = {str(x.get("target") or x.get("offset")) for x in u.get("declared") or [] if isinstance(x, dict)}
    ids = _identities(spec)
    found = []
    for b in blocks:
        d = b.get("detail") if isinstance(b.get("detail"), dict) else {}
        if b["code"] == "verification_failed":
            for t in d.get("targets") or []:
                if str(t) not in have:
                    tid, us = ids["group_target"].get(str(t), (None, []))
                    found.append({"kind": "group_target", "key": str(t), "target": tid, "units": us,
                                  "why": {"block": b["code"], "implausibility": ((rep.get("verification") or {}).get(
                                      "targets") or {}).get(t, {}).get("implausibility")}})
        elif b["code"] == "offset_sampling_low_acceptance":
            n_rows = d.get("rows")
            for c in d.get("by_component") or []:
                for k in (c.get("blocks") or []) if isinstance(c, dict) else []:
                    if str(k) not in have:
                        src, us = ids["offset"].get(str(k), (None, []))
                        nf = len(c.get("rows") or [])
                        found.append({"kind": "offset", "key": str(k), "target": src, "units": us,
                                      "why": {"block": b["code"], "failed_rows": nf, "rows": n_rows,
                                              "failed_share": round(nf / n_rows, 4) if _num(n_rows) and n_rows else None,
                                              "component_blocks": [str(x) for x in c.get("blocks") or []]}})
    units = rep.get("units") or {}
    signed_flag = u.get("mode") == "flag"       # the signed option already lets its unit misses proceed, flagged
    for k in sorted((k for k, v in units.items() if isinstance(v, dict) and v.get("status") != "matched"
                     and not signed_flag), key=str):
        found.append({"kind": "unit", "key": str(k), "target": str(k), "units": [str(k)],
                      "why": {"status": units[k].get("status"), "direction": units[k].get("direction"),
                              "implausibility": units[k].get("implausibility_verified",
                                                             units[k].get("best_implausibility"))}})
    unknown = [c["key"] for c in found if not c["target"] or not c["units"]]
    if unknown:             # Codex r1 P1-2: only an identity the spec's targets generate, with its units, is declared
        out["conflicts"]["refused"] = f"misses that are not a declarable target of this spec: {unknown[:5]}"
        return out
    nd = set(pol["never_declare"])

    def protected(c):       # Codex r1 P1-3: the key, its target or source, and for a unit every unit target on it
        names = {c["key"], c["target"]}
        if c["kind"] == "unit":
            names |= ids["unit_names"] | {f"{t}:{c['key']}" for t in ids["unit_targets"]}
        if c["kind"] == "offset":
            names |= {ids["source_targets"].get(c["target"], c["target"])}
        return bool(names & nd)
    hard = [c["key"] for c in found if protected(c)]
    if hard:
        out["conflicts"]["refused"] = f"never_declare targets cannot be declared: {hard[:5]}"
        return out
    totals = {"group_target": len(ids["group_target"]), "offset": len(ids["offset"]), "unit": len(units)}
    prior = {"group_target": sum(1 for x in u.get("declared") or [] if isinstance(x, dict) and "target" in x),
             "offset": sum(1 for x in u.get("declared") or [] if isinstance(x, dict) and "offset" in x), "unit": 0}
    for kind, tot in totals.items():
        n = sum(c["kind"] == kind for c in found) + prior[kind]     # Codex r2 P1-2: the signed declarations count
        cap = max(1, math.floor(pol["max_share_declared"] * tot + 1e-9))
        if n > cap:
            out["conflicts"]["refused"] = (f"{n} {kind} conflicts exceed max_share_declared "
                                           f"{pol['max_share_declared']:g} of {tot} (at most {cap})")
            return out
    lo, hi = pol["test_scales"]
    for n, c in enumerate(found, 1):
        base = f"{TEST_PREFIX}{_slug(c['kind'] + ':' + c['key'])}"
        c.update(n=n, tests=[f"{base}_lo", f"{base}_hi"], scales=[lo, hi])
    raw2 = copy.deepcopy(spec.raw)
    j2 = raw2["calibration"]["joint"]
    u2 = dict(u)
    u2.update(mode="flag", default_reason=u.get("default_reason") or AUTO_REASON,
              max_units=max(int(u.get("max_units") or 0), sum(c["kind"] == "unit" for c in found)),
              declared=[x for x in u.get("declared") or []] + [
                  {("target" if c["kind"] == "group_target" else "offset"): c["key"], "reason": AUTO_REASON,
                   "decision_test": c["tests"][0], "auto": True} for c in found if c["kind"] != "unit"])
    j2["unmatched"] = u2

    class View:                       # the spec with the automatic declarations, for unmatched_decision only
        raw, uid = raw2, spec.uid
        units = staticmethod(spec.units)
    dec2 = JC.unmatched_decision(View, rep)
    if not dec2.get("proceed"):
        out["conflicts"]["refused"] = f"the report does not allow declaring its misses: {dec2.get('why_not')}"
        return out
    return dict(dec2, conflicts={"policy": pol["policy"], "declared": found, "refused": None})


def tests(conf: dict | None) -> list:
    """The automatic decision tests of the declared conflicts (decision_tests calibration_target)."""
    out = []
    for c in (conf or {}).get("declared") or []:
        for tid, sc in zip(c["tests"], c["scales"]):
            out.append({"id": tid, "type": "calibration_target", "automatic": True, "conflict": c["key"],
                        "calibration_target": {"units": list(c["units"]), "scale": sc},
                        "finding": f"{FINDING_KIND}:{c['key']}",
                        "text": f"Does the plan change when the held-out {c['kind'].replace('_', ' ')} {c['key']}'s "
                                f"units have their calibration targets scaled by {sc:g}?"})
    return out


def refit_estimate(jc: dict | None) -> dict:
    """What a refit without the held-out target would cost, estimated from the joint calibration this run made: one
    more calibration of the same design (its simulator calls; stored rows may cut it). Not run by the harness."""
    ad = (jc or {}).get("adapter") or {}
    calls = ad.get("simulator_calls")
    return {"simulator_calls": calls if _num(calls) else None, "stored_rows_reused": ad.get("stored_rows_reused"),
            "basis": "one joint calibration without the target, of the design this run calibrated: about the "
                     "simulator calls its own calibration made (stored rows of this run may be reused)"}


def refit_option(est: dict | None) -> str:
    calls = (est or {}).get("simulator_calls")
    return ("refit without the target: amend the spec to drop it and relaunch with reuse; not run by the harness; "
            "estimated cost " + (f"about {int(calls)} simulator calls (the joint calibration's own)" if calls is not None
                                 else "one more joint calibration (its size was not recorded)"))


def record(conf: dict | None, dt_records: list, refit: dict | None = None) -> dict:
    """analysis/conflicts.yaml: each declared conflict with its tests' verdicts; `needs_owner` when a test changes the
    plan or did not run (its effect on the plan is then unknown)."""
    by = {str(r.get("test_id")): r for r in dt_records or [] if isinstance(r, dict)}
    rows = []
    for c in (conf or {}).get("declared") or []:
        vs = {}
        for t in c["tests"]:
            r = by.get(t)
            vs[t] = ("NOT_RUN" if r is None or r.get("status") == "not_run" else
                     "CHANGES_PLAN" if r.get("decision_changes") else "NO_CHANGE")
        rows.append(dict({k: c[k] for k in ("n", "kind", "key", "target", "units", "why", "tests", "scales")},
                         verdicts=vs, changes_plan=any(v != "NO_CHANGE" for v in vs.values())))
    return {"policy": (conf or {}).get("policy", "stop"), "declared": rows, "refused": (conf or {}).get("refused"),
            "needs_owner": [r["key"] for r in rows if r["changes_plan"]], "refit_estimate": refit}


def load(run_dir: str) -> dict | None:
    """The payload of analysis/conflicts.yaml, None without one; ValueError when it is unreadable (fail closed)."""
    import yaml
    p = os.path.join(run_dir, ARTIFACT)
    if not os.path.lexists(p):
        return None
    try:
        doc = yaml.safe_load(open(p, encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError) as e:
        raise ValueError(f"{ARTIFACT} cannot be read: {type(e).__name__}") from None
    d = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(d, dict) or not isinstance(d.get("declared"), list):
        raise ValueError(f"{ARTIFACT} has no declared list")
    return d


def _complete_joint_run(run_dir: str) -> bool:
    """A complete analysis (its manifest) that ran the joint calibration: it always writes the conflict record."""
    import json
    try:
        m = json.load(open(os.path.join(run_dir, "analysis", "manifest.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return False
    arts = m.get("artifacts") if isinstance(m, dict) else None
    return bool(isinstance(m, dict) and m.get("status") == "complete" and isinstance(arts, dict)
                and any(str(k).replace(os.sep, "/") == "analysis/joint_calibration.yaml" for k in arts))


def findings(run_dir: str) -> tuple:
    """([finding rows for agents/findings_ledger.py], [problems]): one HIGH `evidence_conflict_declared` per declared
    conflict; open (the owner decides) when its test changes the plan or did not run, else declared (a limitation)."""
    def to_owner(why):
        return [{"id": f"{FINDING_KIND}:record", "kind": FINDING_KIND, "key": "record", "status": "open",
                 "resolution_type": "owner_decision", "claim": f"The conflict record cannot be relied on: {why}.",
                 "fix_requires": "Owner decision: whether any calibration target was held out cannot be read from "
                                 f"{ARTIFACT}; rebuild the analysis, or accept or decline"}]
    try:
        d = load(run_dir)
    except ValueError as e:
        return to_owner(str(e)), [str(e)]
    if d is None and _complete_joint_run(run_dir):
        return to_owner(f"a complete joint-calibration analysis has no {ARTIFACT}"), []
    rows = []
    for c in (d or {}).get("declared") or []:
        if not isinstance(c, dict) or not c.get("key"):
            return [], [f"{ARTIFACT}: a declared row without a key"]
        rows.append({"id": f"{FINDING_KIND}:{c['key']}", "kind": FINDING_KIND, "key": str(c["key"]),
                     "status": "open" if c.get("changes_plan", True) else "declared",
                     "resolution_type": "owner_decision" if c.get("changes_plan", True) else "decision_test",
                     "claim": f"Calibration target {c['key']} ({c.get('kind')}) could not be met and was held out "
                              f"under conflicts.policy declare_and_test; its decision tests {c.get('tests')} gave "
                              f"{c.get('verdicts')}.",
                     "fix_requires": None if not c.get("changes_plan", True) else
                     "Owner decision: the held-out target's test changes the plan (or could not run). Options: accept "
                     "the plan with the target declared; decline; or " + refit_option(d.get("refit_estimate"))})
    return rows, []


def report_lines(run_dir: str) -> list:
    """The report's block: every held-out target, its kind, why and its tests' verdicts."""
    try:
        d = load(run_dir)
    except ValueError as e:
        return ["## Held-out calibration targets (declared evidence conflicts)", "", f"Could not be read: {e}", ""]
    if not d or not d.get("declared"):
        return []
    out = ["## Held-out calibration targets (declared evidence conflicts)", "",
           "Each target below could not be met; the run held it out under the signed conflict policy and tested the "
           "plan without it.", "", "| target | kind | units | tests | changes the plan |", "|---|---|---|---|---|"]
    for c in d["declared"]:
        vs = c.get("verdicts") or {}
        esc = lambda x: str(x).replace("|", "\\|")      # noqa: E731
        ch = "no" if not c.get("changes_plan") else ("unknown (a test did not run)" if "NOT_RUN" in vs.values()
                                                     else "yes")
        out.append(f"| {esc(c.get('key'))} | {c.get('kind')} | {len(c.get('units') or [])} | "
                   f"{esc(', '.join(f'{k}: {v}' for k, v in vs.items()))} | {ch} |")
    return out + [""]


def _self_test() -> int:
    import tempfile
    import yaml
    from toolkit.analysis import joint_calibration as JC
    from toolkit.spec import schema, signoff as SO
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d:
        p = JC._toy_joint(d)
        raw = yaml.safe_load(open(p))
        raw["calibration"]["joint"]["targets"][0].update(bias_by="region", bias_sd=0.1, bias_source="S")
        yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
        for bad, why in (({"policy": "maybe"}, "policy"), ({"policy": "stop", "extra": 1}, "unknown"),
                         ({"policy": "declare_and_test", "max_share_declared": 2}, "max_share"),
                         ({"policy": "declare_and_test", "never_declare": [3]}, "never_declare"),
                         ({"policy": "declare_and_test", "test_scales": [1.2, 0.8]}, "test_scales"), ("x", "mapping")):
            ok(any(why in b for b in spec_problems(dict(raw, conflicts=bad))), f"CF_POLICY_SHAPE refused: {why}")
        ok(spec_problems(dict(raw, conflicts={"policy": "declare_and_test"})) == [] and policy(raw)["policy"] == "stop",
           "CF_DEFAULT_STOP a well-formed policy is accepted; a spec without one keeps today's behaviour (stop)")

        def spec_with(**c):
            r = copy.deepcopy(raw)
            if c is not None and c != {"none": True}:
                r["conflicts"] = dict({"policy": "declare_and_test"}, **c)
            yaml.safe_dump(r, open(p, "w"), sort_keys=False)
            return schema.load(p)
        units = {str(i): {"status": "matched"} for i in range(12)}
        vf = {"code": "verification_failed", "detail": {"units": [], "targets": ["region_t:r1"]}}
        rep_vf = {"blocked": True, "conflicting": [], "units": units, "blocks": [vf]}
        # the real case, written generically: one target in one group blocks 83% of the region-sample rows while every
        # other target matches
        fr = list(range(83))
        off = {"code": "offset_sampling_low_acceptance", "detail": {
            "rows": 100, "failed": 83, "failed_rows": fr, "by_component": [{"blocks": ["S|r2"], "rows": fr}]}}
        rep_off = {"blocked": True, "conflicting": [], "units": units, "blocks": [off]}
        s0 = spec_with(none=True)
        d0 = declare(s0, rep_vf, JC.unmatched_decision(s0, rep_vf))
        ok(not d0["proceed"] and d0["conflicts"]["declared"] == [],
           "JC_UNDECLARED_MISS_BLOCKS_WITHOUT_POLICY an unpredicted group-target miss blocks as today")
        s1 = spec_with(max_share_declared=0.05)
        d1 = declare(s1, rep_vf, JC.unmatched_decision(s1, rep_vf))
        c1 = d1["conflicts"]["declared"]
        ok(d1["proceed"] and [c["key"] for c in c1] == ["region_t:r1"] and c1[0]["units"]
           and all(t.startswith(TEST_PREFIX) for t in c1[0]["tests"]) and len(tests(d1["conflicts"])) == 2,
           f"JC_UNDECLARED_MISS_DECLARED under the policy the miss is declared with two automatic tests ({c1})")
        d2 = declare(s1, rep_off, JC.unmatched_decision(s1, rep_off))
        c2 = d2["conflicts"]["declared"]
        ok(d2["proceed"] and [c["key"] for c in c2] == ["S|r2"] and c2[0]["why"]["failed_share"] == 0.83
           and set(c2[0]["units"]) == {str(i) for i in range(12) if i % 3 == 2}
           and [x.get("offset") for x in d2["declared"]] == ["S|r2"],
           f"JC_ONE_TARGET_MOST_ROWS one offset in one group failing most of the region-sample rows is held out, its "
           f"units tested, and the run continues ({c2})")
        tt = tests(d2["conflicts"])
        ok([t["calibration_target"]["scale"] for t in tt] == [0.8, 1.25] and all(t["type"] == "calibration_target"
                                                                               for t in tt),
           "CF_TESTS_ACROSS_RANGE the automatic tests move the held-out units' targets to each end of test_scales")
        sn = spec_with(never_declare=["S"])
        ok(not declare(sn, rep_off, JC.unmatched_decision(sn, rep_off))["proceed"],
           "JC_NEVER_DECLARE_BLOCKS a conflict on a never_declare target stops the run")
        rep_two = dict(rep_off, blocks=[dict(off, detail=dict(off["detail"], by_component=[
            {"blocks": ["S|r2"], "rows": fr[:40]}, {"blocks": ["S|r0"], "rows": fr[40:]}]))])
        ss = spec_with(max_share_declared=0.0)
        got = declare(ss, rep_two, JC.unmatched_decision(ss, rep_two))
        ok(not got["proceed"] and "max_share_declared" in (got["conflicts"]["refused"] or ""),
           f"JC_SHARE_OVER_STOPS more conflicts of a kind than its share stop the run ({got['conflicts']['refused']})")
        for blks, why in (([off, {"code": "emulator_inadequate", "detail": {}}], "another block"),
                          ([dict(off, detail={"rows": 100, "failed": 83})], "no row breakdown"),
                          ([dict(off, detail=dict(off["detail"], by_component=[{"blocks": ["S|r2"], "rows": fr[:5]}]))],
                           "a breakdown that misses failed rows")):
            got = declare(s1, dict(rep_off, blocks=blks), JC.unmatched_decision(s1, dict(rep_off, blocks=blks)))
            ok(not got["proceed"] and got["conflicts"]["refused"], f"CF_HARD_LIMITS never declared: {why}")
        # Codex r1: only identities the spec's targets generate; protected unit targets; groups the table observes
        bm = dict(rep_vf, blocks=[{"code": "verification_failed", "detail": {"units": [], "targets": ["T2"]}}])
        got = declare(s1, bm, JC.unmatched_decision(s1, bm))
        ok(not got["proceed"] and "not a declarable target" in (got["conflicts"]["refused"] or ""),
           f"CF_UNKNOWN_IDENTITY_REFUSED a failed target that is no group or offset identity is never declared "
           f"({got['conflicts']['refused']})")
        um = dict(rep_vf, blocks=[{"code": "unmatched_units", "detail": {"unmatched": ["5"], "conflicting": []}}],
                  units=dict(units, **{"5": {"status": "unmatched"}}))
        sp_ = spec_with(never_declare=["unit_t"])
        ok(not declare(sp_, rep_off, JC.unmatched_decision(sp_, rep_off))["proceed"],
           "CF_NEVER_DECLARE_OFFSET an offset is protected by its unit target's id (Codex r2)")
        r_ = copy.deepcopy(raw)
        r_["calibration"]["joint"]["unmatched"] = {"mode": "flag", "max_units": 0, "default_reason": "r", "declared": [
            {"target": "region_t:r0", "reason": "signed", "decision_test": "DT1"}]}
        r_["conflicts"] = {"policy": "declare_and_test", "max_share_declared": 0.4}
        yaml.safe_dump(r_, open(p, "w"), sort_keys=False)
        sp2 = schema.load(p)
        two = dict(rep_vf, blocks=[{"code": "verification_failed", "detail": {"units": [],
                                                                              "targets": ["region_t:r0", "region_t:r1"]}}])
        ok(not declare(sp2, two, JC.unmatched_decision(sp2, two))["proceed"],
           "CF_SHARE_COUNTS_SIGNED signed declarations count toward the share (Codex r2)")
        for nd in (["unit_t"], ["unit_t:5"], ["5"], ["S"]):
            sp_ = spec_with(never_declare=nd)
            ok(not declare(sp_, um, JC.unmatched_decision(sp_, um))["proceed"],
               f"CF_NEVER_DECLARE_UNIT a unit miss is protected by its unit target, target:unit or unit id ({nd})")
        sp_ = spec_with()
        ok(declare(sp_, um, JC.unmatched_decision(sp_, um))["proceed"], "CF_UNIT_MISS_DECLARED otherwise it is declared")
        tab = open(os.path.join(d, "region_targets.csv")).read().splitlines()
        open(os.path.join(d, "region_targets.csv"), "w").write("\n".join(tab[:2]) + "\n")      # one observed group
        ok(list(_identities(spec_with()).get("group_target")) == ["region_t:" + tab[1].split(",")[0]],
           "CF_SHARE_DENOMINATOR a group target's identities are only the groups its own table observes")
        open(os.path.join(d, "region_targets.csv"), "w").write("\n".join(tab) + "\n")
        ok(any("conflict_" in b for b in spec_problems(dict(raw, conflicts={"policy": "declare_and_test"}, analysis=dict(
            raw["analysis"], decision_tests=[{"id": "conflict_x_lo"}])))),
           "CF_RESERVED_IDS a declared decision test may not take an automatic test's id")
        rec = record(d2["conflicts"], [{"test_id": tt[0]["id"], "status": "run", "decision_changes": False},
                                       {"test_id": tt[1]["id"], "status": "run", "decision_changes": True}])
        rec2 = record(d2["conflicts"], [{"test_id": tt[0]["id"], "status": "run", "decision_changes": False}])
        rec3 = record(d2["conflicts"], [{"test_id": t["id"], "status": "run", "decision_changes": False} for t in tt])
        est = refit_estimate({"adapter": {"simulator_calls": 240, "stored_rows_reused": 0}})
        rec = dict(rec, refit_estimate=est)
        ok("about 240 simulator calls" in refit_option(est) and "was not recorded" in refit_option(refit_estimate(None)),
           "CF_REFIT_OPTION the owner's options name a refit without the target, with its estimated cost")
        ok(rec["needs_owner"] == ["S|r2"] and rec2["needs_owner"] == ["S|r2"] and rec3["needs_owner"] == [],
           "T_CONFLICT_VERDICT a conflict needs the owner when a test changes the plan or did not run, never otherwise")
        os.makedirs(os.path.join(d, "analysis"), exist_ok=True)
        for r_, want in ((rec, ("open", "owner_decision")), (rec3, ("declared", "decision_test"))):
            yaml.safe_dump({"data": r_}, open(os.path.join(d, ARTIFACT), "w"))
            fs, pr = findings(d)
            ok(pr == [] and [(f["status"], f["resolution_type"]) for f in fs] == [want]
               and (want[0] != "open" or "refit without the target" in fs[0]["fix_requires"])
               and fs[0]["kind"] == FINDING_KIND and "| S\\|r2 |" in "\n".join(report_lines(d)),
               f"CF_FINDING_AND_REPORT the finding is {want} and the report lists the held-out target")
        sys.path.insert(0, os.path.join(ROOT, "agents"))
        import findings_ledger as FLG
        fid = f"{FINDING_KIND}:S|r2"
        seen = []
        for r_ in (rec3, rec, rec3):        # Codex r1 P1-5: the current test decides, until the owner answers
            yaml.safe_dump({"data": r_}, open(os.path.join(d, ARTIFACT), "w"))
            FLG.sync(d, None, 1)
            seen.append(FLG.load(d)[fid].status)
        ok(seen == ["declared", "open", "declared"], f"CF_LEDGER_FOLLOWS_TEST a rebuilt conflict whose test now changes "
                                                     f"the plan is open again for the owner ({seen})")
        os.remove(os.path.join(d, ARTIFACT))
        import json
        json.dump({"status": "complete", "artifacts": {"analysis/joint_calibration.yaml": {}}},
                  open(os.path.join(d, "analysis", "manifest.json"), "w"))
        fs, _ = findings(d)
        ok([(f["key"], f["status"]) for f in fs] == [("record", "open")],
           "CF_RECORD_MISSING_TO_OWNER a complete joint run without its conflict record goes to the owner (Codex r2)")
        open(os.path.join(d, ARTIFACT), "w").write("data: [: bad")
        ok(findings(d)[1] and "Could not be read" in "\n".join(report_lines(d)),
           "CF_ARTIFACT_UNREADABLE_FAILS_CLOSED an unreadable conflicts record is a problem, and the report says so")
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d)
        ok(ensure_default(d) and policy(yaml.safe_load(open(p)))["policy"] == "declare_and_test"
           and not ensure_default(d), "CF_DEFAULT_FOR_NEW_SPECS a draft without a policy gets declare_and_test, once")
        r = yaml.safe_load(open(p))
        r["conflicts"] = {"policy": "stop"}
        yaml.safe_dump(r, open(p, "w"), sort_keys=False)
        ok(not ensure_default(d) and policy(yaml.safe_load(open(p)))["policy"] == "stop",
           "CF_STOP_KEPT a draft that states stop keeps it")
        SO.sign(d, "lead", note="self-test")
        r["conflicts"] = {"policy": "declare_and_test"}
        yaml.safe_dump(r, open(p, "w"), sort_keys=False)
        ok(SO.status(d) == "changed", "SG_POLICY_IN_SIGNED_CLOSURE the policy is signed with the spec: changing it "
                                      "after signing is a changed spec")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(2)
