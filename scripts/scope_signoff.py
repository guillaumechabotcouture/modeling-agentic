#!/usr/bin/env python3
"""Phase 49 β (A21): findings that bear on the primary decision go to the owner.

    python3 scripts/scope_signoff.py show <run_dir>       # what the owner is asked
    python3 scripts/scope_signoff.py status <run_dir>     # none | awaiting | answered
    python3 scripts/scope_signoff.py sign <run_dir> --answers answers.yaml [--note "..."]
    python3 scripts/scope_signoff.py sign <run_dir> --accept-all | --decline-all [--note "..."]
    python3 scripts/scope_signoff.py --self-test

E7 attempt 8 scope-declared, by the lead alone, the two findings that most
threatened its answer: the allocation did not hold out of sample (the
question asks for a package per LGA) and two IRS benchmarks drifted (the plan
recommends no IRS). Phase 49 α requires a decision test for each declaration;
this module decides who may accept one. The signed decision brief
(`decision_brief.yaml`) names the decision and its unit. A HIGH gate finding
whose kind bears on that decision (`decision_bearing:` in
`.claude/orchestration/scope_declaration_policy.yaml`: a per-unit allocation
finding when the run allocates; a benchmark drift whose benchmark names an
intervention the decision chooses among) cannot be self-declared.

How it runs
-----------
* The gate (`gate_findings`, before the decision-test check) marks each such
  finding `owner_signoff: true` with an `owner_item` (the finding, why it bears
  on the decision, its declarations and decision-test result, a fingerprint).
  `_recompute_action` turns a DECLARE_SCOPE that has a marked finding into
  AWAIT_OWNER. The validator CLI writes `scope_signoff_request.json` (`record`)
  when the decision is AWAIT_OWNER, and marks an older request superseded
  otherwise.
* `--signoff owner` (main.py): the writer is held, the lead ends its turn with
  AWAITING_SIGNOFF, and the harness waits for the owner (`scope_signoff.md`;
  `sign` from the owner's shell, or `cloud/run_cluster.py <config> sign`), then
  resumes the lead with the answers, like the decision-brief sign-off. A wait
  that times out ends the run `awaiting_scope_signoff` (resumable, exit 2).
* `--signoff lead` (smokes): the lead reviews and writes its answers to
  `scope_signoff_answers.yaml`; the harness signs them `by: lead`.
* Only the harness records an answer (`harness_record`, from main.py, after the
  owner signed outside the agent session): the ledger holds the answer's hash
  under a nonce the harness also writes to trace.jsonl. An answer without that
  record does not count, so a signature an agent wrote is not an approval; no
  script an agent runs is credited with writing it (agents/integrity.py).
* `--signoff none`: no sign-off is asked for; the findings are declarable as
  before (the decision test of Phase 49 α still applies).
* An accepted finding is declarable again (its α decision test is replaced by
  the owner's acceptance when it had none). A declined one is HIGH
  `scope_declaration_declined`, not declarable: fix it while rounds remain; at
  exhaustion the run ends `completed_scope_declined` (exit 2). An answer binds
  to the item's fingerprint (the finding, the declaration entries, the test
  files' hashes): a changed declaration or test is asked again.

Findings (not scope-declarable):
  scope_declaration_declined  HIGH  the owner declined the declaration of this finding
  scope_signoff_invalid       HIGH  scope_signoff.json cannot be read (no answer counts)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import sys
from typing import Callable, Optional

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from lib import scope_declarations as sd  # noqa: E402

REQUEST, SIGNOFF, MD = "scope_signoff_request.json", "scope_signoff.json", "scope_signoff.md"
# Codex review (#85): the harness's record of each answer it received from outside the agent
# session (the owner's CLI or `cloud/run_cluster.py sign`), and the lead's answers file under
# --signoff lead. Agents never write the signature or the ledger: no script run by an agent is
# credited with them (agents/integrity.py), and an answer counts only when the ledger records
# its hash under a nonce the harness also wrote to trace.jsonl.
LEDGER, LEAD_ANSWERS, TRACE = "scope_signoff_ledger.json", "scope_signoff_answers.yaml", "trace.jsonl"
MAX_SIGNOFF_WAITS = 20
DECLINED, INVALID = "scope_declaration_declined", "scope_signoff_invalid"
POLICY = os.path.join(os.path.dirname(HERE), ".claude", "orchestration", "scope_declaration_policy.yaml")
CONDITIONS = ("allocation", "benchmark_intervention", "always", "artifact")
DECISIONS = ("accept", "decline")
ACTION = "AWAIT_OWNER"
_TOKEN = re.compile(r"[a-z][a-z0-9]*")


def _dt_mod():
    from lib.siblings import load_private
    return load_private("declaration_tests", "_ma_p49_")


def _sibling(name: str):
    from lib.siblings import load_private
    return load_private(name, "_ma_p49_")


def load_map(path: str = POLICY) -> dict:
    """{'kinds': {kind: {when, reason}}, 'generic': set}. Raises ValueError when malformed."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        raise ValueError(f"{os.path.basename(path)} cannot be read: {e}") from e
    db = doc.get("decision_bearing") if isinstance(doc, dict) else None
    kinds = db.get("kinds") if isinstance(db, dict) else None
    if not isinstance(kinds, dict) or not kinds:
        raise ValueError(f"{os.path.basename(path)} needs `decision_bearing.kinds:`")
    for k, v in kinds.items():
        if not (isinstance(v, dict) and v.get("when") in CONDITIONS and isinstance(v.get("reason"), str)
                and v["reason"].strip()):
            raise ValueError(f"decision_bearing.kinds.{k} needs `when:` ({', '.join(CONDITIONS)}) and `reason:`")
        if v["when"] == "artifact" and not (isinstance(v.get("artifacts"), list) and v["artifacts"]
                                            and all(isinstance(a, str) for a in v["artifacts"])):
            raise ValueError(f"decision_bearing.kinds.{k}: `when: artifact` needs an `artifacts:` list")
    gen = db.get("generic_tokens", [])
    if not isinstance(gen, list) or not all(isinstance(t, str) for t in gen):
        raise ValueError("decision_bearing.generic_tokens must be a list of strings")
    return {"kinds": kinds, "generic": {t.lower() for t in gen}}


def _tokens(text, generic: set) -> set[str]:
    return {t for t in _TOKEN.findall(str(text or "").lower()) if len(t) >= 2 and t not in generic}


def _read_yaml(run_dir: str, rel: str):
    p = os.path.join(run_dir, rel)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def interventions(run_dir: str, generic: set) -> tuple[set[str], list[str]]:
    """(tokens, names) of the interventions the decision chooses among: package
    components and effect components (effect_concordance.yaml), intervention
    names (eligibility_manifest.yaml). Unreadable files are skipped."""
    names: list[str] = []
    try:
        ec = _read_yaml(run_dir, "models/effect_concordance.yaml") or {}
        for comps in (ec.get("packages") or {}).values() if isinstance(ec.get("packages"), dict) else ():
            names += [str(c) for c in comps or [] if isinstance(c, (str, int))]
        names += [str(e.get("component")) for e in ec.get("effects") or [] if isinstance(e, dict) and e.get("component")]
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError, AttributeError):
        pass
    try:
        em = _read_yaml(run_dir, "models/eligibility_manifest.yaml") or {}
        names += [str(i.get("name")) for i in em.get("interventions") or [] if isinstance(i, dict) and i.get("name")]
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError, AttributeError):
        pass
    names = sorted(set(names))
    return {t for n in names for t in _tokens(n.replace("_", " "), generic)}, names


def _benchmark(run_dir: str, bid: str) -> Optional[dict]:
    try:
        doc = _read_yaml(run_dir, "models/benchmark_targets.yaml")
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError):
        return None
    items = doc.get("benchmarks") if isinstance(doc, dict) else doc
    for b in items or []:
        if isinstance(b, dict) and str(b.get("id")) == bid:
            return b
    return None


def _matches(bench_tokens: set[str], vocab: set[str]) -> list[str]:
    """Vocabulary tokens a benchmark token equals, or starts with (3+ letters)."""
    return sorted({v for v in vocab for t in bench_tokens if t == v or (len(v) >= 3 and t.startswith(v))})


def bears(run_dir: str, v: dict, cfg: dict, ctx: dict) -> Optional[str]:
    """Why this finding bears on the primary decision, or None."""
    kind = str(v.get("kind") or "")
    base = kind[:-len("_persistent")] if kind.endswith("_persistent") else kind
    rule = cfg["kinds"].get(base)
    if not rule:
        return None
    if rule["when"] == "always":
        return rule["reason"]
    if rule["when"] == "artifact":
        aid = str(v.get("artifact") or "")
        return f"{rule['reason']}: `{aid}`" if aid in rule["artifacts"] else None
    if rule["when"] == "allocation":
        if "allocation" not in ctx:
            try:
                ctx["allocation"] = _sibling("alloc_toolkit_use").allocation_present(run_dir)
            except Exception:  # noqa: BLE001 — fail closed: cannot rule the allocation out
                ctx["allocation"] = True
        return rule["reason"] if ctx["allocation"] else None
    key = _dt_mod().key_of(v)
    if "vocab" not in ctx:
        ctx["vocab"], ctx["names"] = interventions(run_dir, cfg["generic"])
    b = _benchmark(run_dir, key) if key else None
    if b is None:
        return f"{rule['reason']} (benchmark `{key or '?'}` cannot be read, so it cannot be ruled out)"
    if not ctx["vocab"]:
        return f"{rule['reason']} (the run lists no interventions, so `{key}` cannot be ruled out)"
    if str(b.get("intervention", "")).strip().lower() in ("none", "null"):
        return None          # the target says it concerns no intervention (a calibration target)
    hit = _matches(_tokens(b.get("intervention") or b.get("metric"), cfg["generic"]), ctx["vocab"])
    if not hit:
        return None
    return f"{rule['reason']}: `{key}` ({str(b.get('intervention') or b.get('metric'))[:80]}) concerns {', '.join(hit)}"


def _file_sha(run_dir: str, rel) -> Optional[str]:
    try:
        with open(os.path.join(run_dir, str(rel)), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except (OSError, TypeError):
        return None


def build_item(run_dir: str, v: dict, reason: str, entries: list[dict], evidence: Optional[str] = None,
               cover: Optional[list] = None) -> dict:
    """What the owner is shown for one finding, with its fingerprint."""
    dt = _dt_mod()
    ident = dt._ident(v)
    cover = dt.covering(entries, v) if cover is None else cover
    decls, tests = [], {}
    for e in cover:
        status, detail, res = dt.entry_status(run_dir, e, v)
        t = {"status": status}
        if status in ("tested", "changes"):
            t.update({"decision_test": detail, "decision_changes": res["decision_changes"],
                      "criterion": res.get("criterion"), "varied": res.get("varied"),
                      "recommendation": res.get("recommendation")})
            tests[detail] = _file_sha(run_dir, detail)
        elif status in ("exempt", "invalid"):
            t["detail"] = detail
        decls.append({"id": str(e.get("id") or ""), "test": t,
                      "text": {f: str(e.get(f))[:1200] for f in sd.JUSTIFICATION_FIELDS if e.get(f)}})
    # Codex review (#85): the evidence is part of what the owner approves. The finding's own
    # result (its claim: observed values, metrics; for a persistence escalation the base
    # finding's), each declaration and the decision-test files' contents.
    fp = hashlib.sha256(json.dumps({"finding": ident, "evidence": " ".join(str(evidence or v.get("claim") or "").split()),
                                    "entries": sorted(cover, key=lambda e: str(e.get("id"))),
                                    "tests": tests}, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return {"finding": ident, "kind": v.get("kind"), "reason": reason, "claim": str(v.get("claim") or "")[:600],
            "declarations": decls, "declared": bool(cover), "fingerprint": fp}


def _signer_ok(by, mode: Optional[str]) -> bool:
    return by == "owner" or (by == "lead" and mode == "lead")


def load_answers(run_dir: str) -> tuple[dict, list[dict]]:
    """({fingerprint: answer}, findings). An unreadable record fails closed."""
    p = os.path.join(run_dir, SIGNOFF)
    if not os.path.exists(p):
        return {}, []
    try:
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
        ans = doc.get("answers")
        if not isinstance(ans, dict) or not all(isinstance(a, dict) and a.get("decision") in DECISIONS
                                                for a in ans.values()):
            raise ValueError("`answers` must map fingerprints to {decision: accept|decline, ...}")
    except (OSError, UnicodeDecodeError, ValueError, AttributeError) as e:
        return {}, [{"kind": INVALID, "severity": "HIGH", "stage": "GATE", "target_stage": "GATE",
                     "scope_declarable": False,
                     "claim": f"{SIGNOFF} cannot be read ({str(e)[:160]}); no owner answer counts. It is written by "
                              f"`scripts/scope_signoff.py sign`; restore it or sign again."}]
    return ans, []


def _answer_sha(a: dict) -> str:
    return hashlib.sha256(json.dumps(a, sort_keys=True, default=str).encode()).hexdigest()


def _trace_nonces(run_dir: str) -> set:
    out = set()
    try:
        with open(os.path.join(run_dir, TRACE), encoding="utf-8") as f:
            for line in f:
                if '"scope_signoff_recorded"' in line:
                    try:
                        out.add(json.loads(line).get("nonce"))
                    except ValueError:
                        continue
    except (OSError, UnicodeDecodeError):
        pass
    return out


def verified_answers(run_dir: str) -> tuple[dict, list[dict]]:
    """({fingerprint: answer}) for the answers the harness recorded: the ledger holds
    the answer's hash under a nonce that trace.jsonl also holds. Findings for an
    unreadable signature or ledger (fail closed: nothing counts)."""
    answers, bad = load_answers(run_dir)
    if bad or not answers:
        return {}, bad
    p = os.path.join(run_dir, LEDGER)
    if not os.path.exists(p):
        return {}, []
    try:
        with open(p, encoding="utf-8") as f:
            led = json.load(f).get("entries")
        if not isinstance(led, dict):
            raise ValueError("`entries` must be a mapping")
    except (OSError, UnicodeDecodeError, ValueError, AttributeError) as e:
        return {}, [{"kind": INVALID, "severity": "HIGH", "stage": "GATE", "target_stage": "GATE",
                     "scope_declarable": False,
                     "claim": f"{LEDGER} cannot be read ({str(e)[:160]}); no owner answer counts. Only the harness "
                              f"writes it, when it receives the owner's answers."}]
    nonces = _trace_nonces(run_dir)
    return {fp: a for fp, a in answers.items()
            if isinstance(led.get(fp), dict) and led[fp].get("sha256") == _answer_sha(a)
            and led[fp].get("nonce") in nonces}, []


def harness_record(run_dir: str) -> str:
    """The harness (main.py), never an agent: record the answers now in scope_signoff.json
    in the ledger, under a fresh nonce also written to trace.jsonl. Returns the nonce."""
    import secrets
    answers, bad = load_answers(run_dir)
    if bad:
        raise ValueError(bad[0]["claim"])
    p = os.path.join(run_dir, LEDGER)
    try:
        with open(p, encoding="utf-8") as f:
            led = json.load(f).get("entries") or {}
    except (OSError, ValueError, AttributeError):
        led = {}
    nonce = secrets.token_hex(16)
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    for fp, a in answers.items():
        if not (isinstance(led.get(fp), dict) and led[fp].get("sha256") == _answer_sha(a)):
            led[fp] = {"sha256": _answer_sha(a), "nonce": nonce, "by": a.get("by"), "recorded_at": now}
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"entries": led, "producer": "main.py"}, f, indent=1)
    with open(os.path.join(run_dir, TRACE), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now, "type": "scope_signoff_recorded", "nonce": nonce}) + "\n")
    return nonce


def lead_answers(run_dir: str) -> Optional[dict]:
    """--signoff lead: the lead's answers file (finding -> accept|decline), or None."""
    try:
        doc = _read_yaml(run_dir, LEAD_ANSWERS)
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError):
        return None
    return doc if isinstance(doc, dict) and doc else None


def gate_findings(run_dir: str, round_n: Optional[int] = None, current: Optional[list] = None,
                  non_declarable: Callable[[str, object], bool] = lambda k, e: e is False) -> list[dict]:
    """Mark each decision-bearing HIGH finding of `current` for the owner (in
    place), and report declined ones. Nothing under --signoff none."""
    mode = os.environ.get("MA_SIGNOFF_MODE")
    if mode == "none":
        return []
    cfg = load_map()
    dt = _dt_mod()
    answers, out = verified_answers(run_dir)
    _doc, entries, problems = sd.load(run_dir)
    if problems:
        entries = []
    ctx: dict = {}
    seen = set()
    todo = [(x, None) for x in (current or []) if isinstance(x, dict)]
    todo += [(x, [e]) for x, e in _not_applicable_analyses(run_dir, entries, cfg, ctx)]
    for v, na_entry in todo:
        if v.get("severity") != "HIGH" or dt.critic_derived(v) \
                or non_declarable(str(v.get("kind") or ""), v.get("scope_declarable")):
            continue
        reason = bears(run_dir, v, cfg, ctx)
        if not reason:
            continue
        base = [x for x in current or [] if isinstance(x, dict) and str(v.get("kind")).endswith("_persistent")
                and x.get("kind") == str(v.get("kind"))[:-len("_persistent")]]
        item = build_item(run_dir, v, reason, entries, evidence=base[0].get("claim") if base else None,
                          cover=na_entry)
        if item["finding"] in seen:
            continue
        seen.add(item["finding"])
        ans = answers.get(item["fingerprint"])
        if ans and _signer_ok(ans.get("by"), mode):
            if ans["decision"] == "accept":
                v["owner_signed"] = {"by": ans.get("by"), "at": ans.get("at"), "fingerprint": item["fingerprint"]}
                continue
            v["owner_declined"] = True
            out.append({"kind": DECLINED, "severity": "HIGH", "stage": "GATE", "target_stage": "MODEL",
                        "scope_declarable": False, "key": item["finding"],
                        "claim": (f"The {ans.get('by')} declined the scope declaration of `{item['finding']}` "
                                  f"({ans.get('at')}): {str(ans.get('note') or 'no note')[:300]}. It bears on the "
                                  f"decision ({reason[:200]}). Fix the finding; a changed declaration or decision "
                                  "test is put to the owner again.")})
            continue
        v["owner_signoff"] = True
        v["owner_item"] = item
        if na_entry is not None:
            out.append(v)          # the declared-absent analysis is a HIGH until the owner accepts it
    return out


def _not_applicable_analyses(run_dir: str, entries: list[dict], cfg: dict, ctx: dict) -> list:
    """[(finding, entry)] for `not_applicable: <artifact>` declarations of a decision
    analysis (the `artifact` rule's list) that the run requires and does not have:
    declaring it absent is the owner's call, like the missing-artifact finding itself."""
    rule = next((r for r in cfg["kinds"].values() if r["when"] == "artifact"), None)
    if rule is None:
        return []
    kind = next(k for k, r in cfg["kinds"].items() if r is rule)
    from lib.rigor_artifacts import load_artifacts
    arts = {a.id: a for a in load_artifacts()}
    out = []
    for e in entries:
        na = e.get("not_applicable")
        if not na or not sd.entry_has_justification(e):
            continue
        for aid in (na if isinstance(na, list) else [na]):
            aid = str(aid).strip()
            a = arts.get(aid)
            if aid not in rule["artifacts"] or a is None or os.path.exists(os.path.join(run_dir, a.path)):
                continue
            if a.triggered_by == "allocation":
                if "allocation" not in ctx:
                    try:
                        ctx["allocation"] = _sibling("alloc_toolkit_use").allocation_present(run_dir)
                    except Exception:  # noqa: BLE001 — fail closed
                        ctx["allocation"] = True
                if not ctx["allocation"]:
                    continue
            out.append(({"kind": kind, "severity": "HIGH", "stage": "GATE", "target_stage": "MODEL",
                         "artifact": aid, "key": aid, "scope_declarable": True,
                         "claim": (f"`{aid}` ({a.path}) is declared not applicable in scope_declaration.yaml (entry "
                                   f"{e.get('id') or '?'}). It is a decision analysis, so only the owner may accept "
                                   "its absence; or produce it.")}, e))
    return out


def record(run_dir: str, decision: dict, round_n: Optional[int] = None) -> Optional[str]:
    """Write the request when the decision waits for the owner; mark an older
    pending request superseded otherwise. Returns the path written, if any."""
    p = os.path.join(run_dir, REQUEST)
    items = [b["owner_item"] for b in decision.get("unresolved_high") or []
             if isinstance(b, dict) and b.get("owner_signoff") and isinstance(b.get("owner_item"), dict)]
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    if decision.get("action") == ACTION and items:
        doc = {"pending": True, "round": round_n, "requested_at": now, "items": items,
               "decision_rationale": decision.get("rationale"), "producer": "scripts/validate_critique_yaml.py"}
    else:
        old = _load_request(run_dir)
        if not old or not old.get("pending"):
            return None
        doc = dict(old, pending=False, superseded_at=now, superseded_by=decision.get("action"))
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    os.replace(tmp, p)
    return p


def _load_request(run_dir: str) -> Optional[dict]:
    try:
        with open(os.path.join(run_dir, REQUEST), encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def status(run_dir: str, verified: bool = True) -> str:
    """none (nothing asked) | awaiting | answered. verified=False: answers written but not
    yet recorded by the harness (what the harness's wait looks for)."""
    req = _load_request(run_dir)
    if not req or not req.get("pending"):
        return "none"
    answers, bad = verified_answers(run_dir) if verified else load_answers(run_dir)
    if bad:
        return "awaiting"
    mode = os.environ.get("MA_SIGNOFF_MODE")
    done = all(_signer_ok((answers.get(i.get("fingerprint")) or {}).get("by"), mode)
               for i in req.get("items") or [])
    return "answered" if done else "awaiting"


def _test_md(t: dict) -> str:
    s = t.get("status")
    if s in ("tested", "changes"):
        rec = t.get("recommendation") or {}
        return (f"decision test `{t.get('decision_test')}`: decision_changes **{t.get('decision_changes')}**; "
                f"varied: {t.get('varied')}; criterion: {t.get('criterion')}; with: {rec.get('with')}; "
                f"without: {rec.get('without')}")
    if s == "exempt":
        return f"no decision test ({t.get('detail')})"
    if s == "invalid":
        return f"decision test not usable: {t.get('detail')}"
    return "**untested**: no decision test"


def render_md(run_dir: str) -> str:
    req = _load_request(run_dir) or {}
    brief = {}
    try:
        brief = _read_yaml(run_dir, "decision_brief.yaml") or {}
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError):
        pass
    lines = ["# Scope declarations that need the owner's sign-off", "",
             f"Decision (signed brief): {brief.get('decision', '(no decision_brief.yaml)')}", "",
             "The run would end with these findings declared as limitations. Each bears on the decision, so "
             "the lead cannot accept it alone. Accept a declaration if the decision stands with it; decline it "
             "if the finding must be fixed.", ""]
    for i, it in enumerate(req.get("items") or [], 1):
        lines += [f"## {i}. `{it.get('finding')}`", "", f"Why it bears on the decision: {it.get('reason')}", "",
                  f"Gate finding: {it.get('claim')}", ""]
        if not it.get("declarations"):
            lines += ["Not declared in scope_declaration.yaml, and **untested**.", ""]
        for dcl in it.get("declarations") or []:
            lines += [f"- Declaration {dcl.get('id')}: {_test_md(dcl.get('test') or {})}"]
            for f, txt in (dcl.get("text") or {}).items():
                lines += [f"  - {f}: {txt}"]
        lines += ["", f"Answer key: `{it.get('finding')}` (fingerprint {it.get('fingerprint')})", ""]
    lines += ["## How to answer", "", "Write answers.yaml, one line per finding:", "", "```yaml"]
    lines += [f"\"{it.get('finding')}\": accept   # or: decline" for it in req.get("items") or []]
    lines += ["```", "", "then `python3 scripts/scope_signoff.py sign <run_dir> --answers answers.yaml --note \"...\"` "
              "(or `--accept-all` / `--decline-all`); on a cloud run, `python cloud/run_cluster.py <config> sign "
              "answers.yaml [note]`."]
    return "\n".join(lines) + "\n"


def sign(run_dir: str, by: str, answers: Optional[dict] = None, all_decision: Optional[str] = None,
         note: str = "") -> dict:
    """Record the answers to the pending request. Raises ValueError when there is
    nothing to sign or an item has no answer."""
    req = _load_request(run_dir)
    if not req or not req.get("pending") or not req.get("items"):
        raise ValueError("no scope declaration is waiting for sign-off")
    prior, bad = load_answers(run_dir)
    if bad:
        prior = {}
    answers = answers or {}
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    new, missing = {}, []
    for it in req["items"]:
        a = answers.get(it["finding"], answers.get(it["fingerprint"], all_decision))
        a_note = note
        if isinstance(a, dict):
            a, a_note = a.get("decision"), str(a.get("note") or note)
        a = str(a).strip().lower() if a is not None else None
        if a not in DECISIONS:
            missing.append(it["finding"])
            continue
        new[it["fingerprint"]] = {"finding": it["finding"], "decision": a, "by": by, "at": now, "note": a_note,
                                  "round": req.get("round")}
    if missing:
        raise ValueError("no accept/decline answer for " + ", ".join(missing))
    doc = {"answers": {**prior, **new}, "last_signed_by": by, "last_signed_at": now,
           "producer": "scripts/scope_signoff.py"}
    with open(os.path.join(run_dir, SIGNOFF), "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    return {"signed_by": by, "signed_at": now, "answers": new}


def answer_text(run_dir: str) -> str:
    """What the lead is told after the sign-off."""
    req = _load_request(run_dir) or {}
    answers, _ = verified_answers(run_dir)
    lines = []
    for it in req.get("items") or []:
        a = answers.get(it.get("fingerprint")) or {}
        lines.append(f"- {it.get('finding')}: {a.get('decision', 'no answer')} by {a.get('by', '?')}"
                     + (f" ({a['note']})" if a.get("note") else ""))
    acc = all((answers.get(it.get("fingerprint")) or {}).get("decision") == "accept" for it in req.get("items") or [])
    lines.append("Re-run the STAGE 7 gate now. "
                 + ("Every declaration was accepted: continue to the write-up the gate gives, and state each "
                    "accepted declaration, its decision test (or that it had none) and the owner's acceptance "
                    "in report.md Limitations." if acc else
                    "A declined declaration is HIGH `scope_declaration_declined`: fix those findings in the "
                    "remaining rounds; the accepted ones stand."))
    return "\n".join(lines)


# --------------------------------------------------------------------------- self-test

def _self_test() -> int:
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

    cfg = load_map()
    old_mode = os.environ.get("MA_SIGNOFF_MODE")
    os.environ.pop("MA_SIGNOFF_MODE", None)

    def cur():
        return [{"kind": "allocation_unstable", "severity": "HIGH", "stage": "OPTIMIZATION",
                 "key": "allocation_robustness", "claim": "UNSTABLE"},
                {"kind": "benchmark_drift", "severity": "HIGH", "stage": "MODEL", "key": "B15", "claim": "B15 drift"},
                {"kind": "benchmark_drift", "severity": "HIGH", "stage": "MODEL", "key": "B1", "claim": "B1 drift"},
                {"kind": "weakly_identified_parameters_persistent", "severity": "HIGH", "stage": "GATE"}]

    try:
        with tempfile.TemporaryDirectory() as d:
            w(d, "models/benchmark_targets.yaml", {"benchmarks": [
                {"id": "B1", "metric": "national_prevalence_2021"},
                {"id": "B15", "metric": "net_spray_interaction_or"},
                {"id": "B20", "metric": "netplus_incremental_usd_per_daly_vs_std"}]})
            w(d, "models/effect_concordance.yaml", {"packages": {"cf": [], "cf+net": ["net"], "cf+net+drug": ["net", "drug"]}})
            w(d, "models/eligibility_manifest.yaml", {"interventions": [{"name": "spray_nonstandard"}]})
            open(os.path.join(d, "decision_rule.md"), "w").write("rule\n")
            ctx = {}
            ok(bears(d, cur()[0], cfg, ctx) is not None, "SO1 allocation_unstable bears when the run allocates")
            ok(bears(d, cur()[1], cfg, ctx) is not None and "spray" in bears(d, cur()[1], cfg, ctx),
               "SO2 a benchmark naming an intervention excluded from the plan (in the eligibility list) bears")
            ok(bears(d, cur()[2], cfg, ctx) is None, "SO3 a calibration benchmark (no intervention token) does not")
            ok(bears(d, {"kind": "benchmark_drift", "key": "B20"}, cfg, ctx) is not None,
               "SO4 a metric token that starts with a component name (netplus ~ net) bears")
            ok(bears(d, cur()[3], cfg, ctx) is None, "SO5 kinds not in the map do not bear")
            w(d, "models/benchmark_targets.yaml", {"benchmarks": [
                {"id": "B1", "metric": "national_prevalence_2021"},
                {"id": "B15", "metric": "net_spray_interaction_or"},
                {"id": "B20", "metric": "netplus_incremental_usd_per_daly_vs_std"},
                {"id": "B30", "metric": "net_coverage_2021", "intervention": "none"},
                {"id": "B31", "metric": "odd_name", "intervention": "spray"}]})
            ok(bears(d, {"kind": "benchmark_drift", "key": "B30"}, cfg, {}) is None
               and bears(d, {"kind": "benchmark_drift", "key": "B31"}, cfg, {}) is not None,
               "SO5b a target's own `intervention:` field decides (none: a calibration target)")
            ok(bears(d, {"kind": "benchmark_drift", "key": "B99"}, cfg, ctx) is not None,
               "SO6 an unreadable benchmark fails closed (bears)")
            os.remove(os.path.join(d, "decision_rule.md"))
            ok(bears(d, cur()[0], cfg, {}) is None, "SO7 no allocation: allocation_unstable does not bear")
            open(os.path.join(d, "decision_rule.md"), "w").write("rule\n")
            # SO25-SO27: a missing decision-relevant analysis is the owner's to accept, not self-exemptable.
            ok(bears(d, {"kind": "sensitivity_analysis_missing", "severity": "HIGH"}, cfg, {}) is not None
               and bears(d, {"kind": "uq_report_missing", "severity": "HIGH"}, cfg, {}) is not None
               and bears(d, {"kind": "claims_ledger_missing", "severity": "HIGH"}, cfg, {}) is None,
               "SO25 a missing decision analysis bears on the decision; a missing document does not")
            ra = {"kind": "required_artifact_missing", "severity": "HIGH", "accept_blocker": True,
                  "artifact": "sensitivity_analysis", "claim": "absent"}
            c = [ra, dict(ra, artifact="claims_ledger")]
            gate_findings(d, 6, c)
            ok(c[0].get("owner_signoff") and not c[1].get("owner_signoff"),
               "SO26 a missing required analysis is put to the owner; a missing document is not")
            w(d, "scope_declaration.yaml", {"declarations": [
                {"id": "SD-NA", "not_applicable": "outcome_fn", "justification": "no model run needed"}]})
            f = gate_findings(d, 6, [])
            ok(len(f) == 1 and f[0]["kind"] == "required_artifact_missing" and f[0].get("owner_signoff")
               and f[0]["artifact"] == "outcome_fn",
               f"SO27 declaring a decision analysis not applicable needs the owner: {[x.get('kind') for x in f]}")
            os.remove(os.path.join(d, "scope_declaration.yaml"))
            # Marking, the decision, the request.
            c = cur()
            ok(gate_findings(d, 6, c) == [] and c[0].get("owner_signoff") and c[1].get("owner_signoff")
               and not c[2].get("owner_signoff") and not c[3].get("owner_signoff"),
               "SO8 decision-bearing findings are marked for the owner, others not")
            ok(c[0]["owner_item"]["declared"] is False and not c[0]["owner_item"]["declarations"],
               "SO9 an undeclared finding is shown as such")
            w(d, "scope_declaration.yaml", {"declarations": [
                {"id": "SD-007", "blocker_id": "allocation_unstable", "why_unresolved": "no data"},
                {"id": "SD-002", "blocker_id": "benchmark_drift", "why_unresolved": "no mechanism",
                 "decision_test": "models/decision_tests/b15.yaml"}]})
            w(d, "models/decision_tests/b15.yaml", {"finding": "B15", "varied": "interaction OR at 2.43",
                                                    "criterion": "package changes in >10% of units",
                                                    "decision_changes": False,
                                                    "recommendation": {"with": "no spray", "without": "no spray"}})
            c = cur()
            gate_findings(d, 6, c)
            t0 = c[0]["owner_item"]["declarations"][0]["test"]["status"]
            t1 = c[1]["owner_item"]["declarations"][0]["test"]
            ok(t0 == "untested" and t1["status"] == "tested" and t1["decision_changes"] is False,
               f"SO10 the owner sees each declaration's test or 'untested': {t0}, {t1}")
            dec = {"action": ACTION, "rationale": "x",
                   "unresolved_high": [{"kind": v["kind"], "owner_signoff": True, "owner_item": v["owner_item"]}
                                       for v in c if v.get("owner_signoff")]}
            record(d, dec, 6)
            ok(status(d) == "awaiting" and "untested" in render_md(d) and "SD-002" in render_md(d),
               "SO11 the request is pending and the page shows tests and 'untested'")
            try:
                sign(d, "owner", {"allocation_unstable:allocation_robustness": "accept"})
                ok(False, "SO12 an item without an answer is refused")
            except ValueError:
                ok(True, "SO12 an item without an answer is refused")
            sign(d, "owner", {"allocation_unstable:allocation_robustness": "decline",
                              "benchmark_drift:B15": {"decision": "accept", "note": "IRS is out either way"}})
            # SO23 (Codex P1): a signature file the harness has not recorded is not an owner answer.
            c = cur()
            gate_findings(d, 6, c)
            ok(c[1].get("owner_signoff") and not c[1].get("owner_signed") and status(d) == "awaiting"
               and status(d, verified=False) == "answered",
               "SO23 an unrecorded signature (an agent could have written it) does not count")
            before = {f: open(os.path.join(d, f)).read() for f in (REQUEST, SIGNOFF)}
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                main(["show", d])
            ok({f: open(os.path.join(d, f)).read() for f in (REQUEST, SIGNOFF)} == before
               and not os.path.exists(os.path.join(d, LEDGER)), "SO23b `show` writes nothing")
            harness_record(d)
            ok(status(d) == "answered", "SO13 answered once every item has a recorded owner answer")
            c = cur()
            f = gate_findings(d, 7, c)
            ok([x["kind"] for x in f] == [DECLINED] and c[1].get("owner_signed") and not c[1].get("owner_signoff")
               and not c[0].get("owner_signoff"), f"SO14 declined -> HIGH, accepted -> declarable: {f}")
            ok("decline by owner" in answer_text(d) and "Re-run" in answer_text(d), "SO15 the lead's resume text")
            # SO24 (Codex P1): changed evidence (the finding's result) is asked again.
            c = cur()
            c[1]["claim"] = "B15 drift: observed 0.57"
            gate_findings(d, 7, c)
            c2 = cur()
            c2[1]["claim"] = "B15 drift: observed 0.99"
            gate_findings(d, 7, c2)
            ok(c[1]["owner_item"]["fingerprint"] != c2[1]["owner_item"]["fingerprint"],
               "SO24 the fingerprint covers the finding's evidence (0.57 -> 0.99 is a new item)")
            # A changed declaration is asked again.
            w(d, "models/decision_tests/b15.yaml", {"finding": "B15", "varied": "interaction OR at 2.0",
                                                    "criterion": "c", "decision_changes": False,
                                                    "recommendation": {"with": "a", "without": "a"}})
            c = cur()
            gate_findings(d, 7, c)
            ok(c[1].get("owner_signoff") and not c[1].get("owner_signed"), "SO16 a changed test is asked again")
            # Lead signatures count only in lead mode.
            record(d, {"action": ACTION, "unresolved_high": [{"owner_signoff": True, "owner_item": c[1]["owner_item"]}]}, 7)
            sign(d, "lead", all_decision="accept")
            harness_record(d)
            c2 = cur()
            gate_findings(d, 7, c2)
            ok(c2[1].get("owner_signoff"), "SO17 a lead signature does not count in owner mode")
            os.environ["MA_SIGNOFF_MODE"] = "lead"
            c2 = cur()
            gate_findings(d, 7, c2)
            ok(c2[1].get("owner_signed", {}).get("by") == "lead", "SO18 ...and counts in lead mode")
            os.environ["MA_SIGNOFF_MODE"] = "none"
            c3 = cur()
            ok(gate_findings(d, 7, c3) == [] and not any(v.get("owner_signoff") for v in c3),
               "SO19 --signoff none: nothing is escalated")
            os.environ.pop("MA_SIGNOFF_MODE", None)
            # Superseded request; unreadable answers fail closed.
            record(d, {"action": "PATCH_OR_RETHINK", "unresolved_high": []}, 8)
            ok(status(d) == "none", "SO20 a later non-AWAIT decision supersedes the request")
            open(os.path.join(d, SIGNOFF), "w").write("{not json")
            ok([x["kind"] for x in gate_findings(d, 8, cur())] == [INVALID], "SO21 an unreadable record is HIGH")
        bad = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        bad.write("decision_bearing:\n  kinds:\n    x: {when: sometimes, reason: r}\n")
        bad.close()
        try:
            load_map(bad.name)
            ok(False, "SO22 a malformed map raises")
        except ValueError:
            ok(True, "SO22 a malformed map raises (the gate guard makes it HIGH)")
        os.remove(bad.name)
    finally:
        if old_mode is None:
            os.environ.pop("MA_SIGNOFF_MODE", None)
        else:
            os.environ["MA_SIGNOFF_MODE"] = old_mode
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", choices=["show", "status", "sign"])
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--by", default="owner", choices=["owner"])
    ap.add_argument("--answers", help="YAML mapping finding -> accept|decline (or {decision, note})")
    ap.add_argument("--accept-all", action="store_true")
    ap.add_argument("--decline-all", action="store_true")
    ap.add_argument("--note", default="")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.cmd or not a.run_dir:
        ap.print_help()
        return 2
    if a.cmd == "show":
        print(render_md(a.run_dir))
        return 0
    if a.cmd == "status":
        print(status(a.run_dir))
        return 0
    if os.environ.get("MA_SIGNOFF_MODE"):
        # Set only in the agents' environment (main.py): the owner signs from a shell of their own.
        # Under --signoff lead the lead writes scope_signoff_answers.yaml and the harness signs.
        print("Agents cannot sign scope declarations. Owner: sign from your own shell. Lead (--signoff lead): "
              f"write {LEAD_ANSWERS} and end your turn with AWAITING_SIGNOFF.", file=sys.stderr)
        return 1
    try:
        answers = yaml.safe_load(open(a.answers, encoding="utf-8")) if a.answers else None
        if answers is not None and not isinstance(answers, dict):
            raise ValueError("--answers must be a YAML mapping")
        rec = sign(a.run_dir, a.by, answers, "accept" if a.accept_all else "decline" if a.decline_all else None,
                   a.note)
    except (ValueError, OSError, yaml.YAMLError) as e:
        print(f"Not signed: {e}", file=sys.stderr)
        return 1
    print(f"Signed by {rec['signed_by']} at {rec['signed_at']}: "
          + ", ".join(f"{x['finding']} {x['decision']}" for x in rec["answers"].values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
