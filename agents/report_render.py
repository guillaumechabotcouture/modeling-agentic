"""Report rendering, status lines, deliverable checks, and claim/dependence blocks (Wave B2).

Imports `agents/v2_gate` (leaf) and never imports `engine_build_stage` or `stage_machine`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_ROOT, "scripts")
for _p in (_ROOT, _SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents import analysis_backend as AB
from agents import findings_ledger as FL
from agents import fix_loop as FX
from agents import v2_gate as VG

STATE_FILE = "run_state.json"
SIGNOFF_FILE = "decision_spec.signoff.json"
SIGNOFF_SHEET = "decision_spec.signoff_sheet.md"
SCOPE_SIGNOFF = "scope_signoff.json"
AMENDMENT_RECORD = "amendment_record.json"
DECLARED_FILE = "declared_findings.yaml"
SPEC_RETHINK_REQUEST = "spec_rethink_request.json"
PROTECTED_BY_FIX = (AB.SPEC_FILE, AB.SETTINGS_FILE)

ACCEPTED_STATUSES = ("completed", "completed_with_report_restored")
DELIVERED_STATUSES = ACCEPTED_STATUSES + ("completed_scope_declared", "completed_scope_declined",
                                          "completed_gate_not_accepted")
STATUS_MARK = "<!-- harness: run status -->"

_STATUS_LINES = {
    "completed": "accepted by the final gate",
    "completed_with_report_restored": "accepted by the final gate",
    "completed_scope_declared": "declared scope, not accepted",
    "pending": "not yet decided by the final gate",
    "other": "not accepted",
}

_remedy, short_label = VG._remedy, VG.short_label


def _put(run_dir: str, rel: str, text: str) -> None:
    """Publish a harness file in run_dir atomically without following links."""
    from lib.safe_io import write_nofollow
    write_nofollow(run_dir, rel.replace(os.sep, "/"), text.encode("utf-8"), atomic=True)


def _write_json(run_dir: str, rel: str, obj) -> None:
    _put(run_dir, rel, json.dumps(obj, indent=1, default=str))


def signed_hash(run_dir: str) -> str | None:
    """sha256 of decision_spec.yaml as a regular single file."""
    p = os.path.join(run_dir, AB.SPEC_FILE)
    if os.path.islink(p):
        return None
    try:
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _spec_signed_sha(run_dir: str) -> str | None:
    """The spec sha256 a sign-off record binds (the spec's own record, or a brief's that hashed the spec), or None."""
    hexd = re.compile(r"[0-9a-f]{64}")

    def nested(r):
        m = r.get("sha256")
        return m.get(AB.SPEC_FILE) if isinstance(m, dict) else (False if m is not None else None)

    def own(r):
        v = r.get("spec_sha256") or nested(r)
        return False if v is None and "spec_sha256" in r else v

    for f, get in ((SIGNOFF_FILE, own), ("decision_brief.signoff.json", nested)):
        p = os.path.join(run_dir, f)
        if not os.path.lexists(p):
            continue
        try:
            rec = json.load(open(p))
        except (OSError, ValueError):
            return "unreadable"
        if not isinstance(rec, dict):
            return "unreadable"
        v = get(rec)
        if v is False or (v is not None and not (isinstance(v, str) and hexd.fullmatch(v))):
            return "unreadable"
        if v:
            return v
    return None


def _withhold_report(run_dir: str) -> None:
    """Move a rendered regular report.md to report_withheld.md (or remove a link at report.md)."""
    p = os.path.join(run_dir, "report.md")
    if os.path.islink(p):
        from lib.safe_io import remove_nofollow
        remove_nofollow(run_dir, "report.md")
    elif os.path.isfile(p):
        from lib.safe_io import rename_nofollow
        rename_nofollow(run_dir, "report.md", "report_withheld.md")


def _retire_stale_withheld(run_dir: str) -> None:
    """Remove any leftover report_withheld.md once a post-write gate delivers a regular report.md."""
    p = os.path.join(run_dir, "report.md")
    w = os.path.join(run_dir, "report_withheld.md")
    if os.path.isfile(p) and not os.path.islink(p) and (os.path.islink(w) or os.path.isfile(w)):
        from lib.safe_io import remove_nofollow
        remove_nofollow(run_dir, "report_withheld.md")


def harness_owned() -> tuple:
    """Run files the harness, gate, or critic owns (glob patterns)."""
    import pipeline as P
    return tuple(P._reserved_patterns()) + PROTECTED_BY_FIX + (
        STATE_FILE, SIGNOFF_FILE, SCOPE_SIGNOFF, "spec_rethink_request.json", FL.LEDGER, FX.IMPACT,
        "report.md", "manuscript.md", "metadata.json", "report_render.json", "report_withheld.md", "final_gate.json",
        AB.ENGINE_CHOICE, "validator_history.jsonl", "gate_r*", "fix_plan_r*", "fix_progress_r*",
        "analysis/*", "models/pipeline*", "evidence/*", "data/*",
        "acquire", "acquire/*", "engine_build", "engine_build/*", "models/ext/engine_*", "models/ext/engine_*/*",
        "evidence_dossier.yaml", SIGNOFF_SHEET, AMENDMENT_RECORD,
    )


def owned_path(root: str, rel: str, owned) -> bool:
    """True when `rel` inside `root` matches any owned glob pattern (case-insensitively) or shares an inode."""
    import glob
    import pipeline as P
    low = rel.lower()
    if any(P.path_match(low, str(pat).lower()) for pat in owned):
        return True
    target = os.path.join(root, rel)
    if not os.path.exists(target):
        return False
    try:
        if os.path.isfile(target) and os.stat(target).st_nlink > 1:
            return True
        for pat in owned:
            for hit in glob.glob(os.path.join(root, str(pat)), recursive=True):
                if os.path.exists(hit) and os.path.samefile(hit, target):
                    return True
    except OSError:
        return True
    return False


def deliverable_problems(run_dir: str) -> list[str]:
    """The spec's `deliverable_file.path` must be a plain relative file inside run_dir not owned by harness."""
    import yaml
    from lib.safe_io import read_nofollow
    try:
        spec = yaml.safe_load(read_nofollow(run_dir, AB.SPEC_FILE)[0]) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError, RecursionError):
        return []
    if not isinstance(spec, dict):
        return []
    df = spec.get("deliverable_file")
    if df is None:
        return []
    rel = df.get("path") if isinstance(df, dict) else None
    if not isinstance(rel, str) or not rel.strip() or not isinstance(df.get("columns"), dict):
        return ["deliverable_file needs a path and columns"]
    parts = rel.replace("\\", "/").split("/")
    if rel != rel.strip() or os.path.isabs(rel) or any(p in ("", ".", "..") for p in parts):
        return [f"deliverable path {rel!r} must be relative and inside the run directory"]
    root = os.path.realpath(run_dir)
    target = os.path.join(root, rel)
    if not os.path.realpath(target).startswith(root + os.sep):
        return [f"deliverable path {rel!r} resolves outside the run directory"]
    cur = root
    for part in parts:
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return [f"deliverable path {rel!r} goes through a symlink ({os.path.relpath(cur, root)})"]
    norm = os.path.relpath(target, root).replace(os.sep, "/")
    if os.path.isdir(target):
        return [f"deliverable path {rel!r} names an existing directory"]
    drel = spec["evidence"].get("dossier") if isinstance(spec.get("evidence"), dict) else None
    if isinstance(drel, str) and drel.strip():
        dn = os.path.normpath(drel).replace(os.sep, "/").lower()
        dp = os.path.join(root, drel)
        try:
            same = os.path.exists(dp) and os.path.exists(target) and os.path.samefile(dp, target)
        except OSError:
            same = True
        if dn == norm.lower() or same:
            return [f"deliverable path {rel!r} is the evidence dossier the spec names"]
    if owned_path(root, norm, harness_owned()):
        return [f"deliverable path {rel!r} is a file the harness, the gate or a critic owns"]
    return []


def _draft_file_problems(run_dir: str) -> list[str]:
    """The spec (required) and analysis_settings.yaml (optional) must be regular single-link files."""
    import stat
    out = []
    for rel, required in ((AB.SPEC_FILE, True), (AB.SETTINGS_FILE, False)):
        try:
            st = os.lstat(os.path.join(run_dir, rel))
        except FileNotFoundError:
            if required:
                out.append(f"spec unreadable: {rel} does not exist")
            continue
        except OSError as e:
            out.append(f"{rel} cannot be read: {type(e).__name__}")
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            out.append(f"{rel} is a link or not a regular file: write it as a plain file")
        elif st.st_nlink > 1:
            out.append(f"{rel} has {st.st_nlink} hard links: write it as its own file")
    return out


def _special_files(run_dir: str, limit: int = 20000) -> list[str]:
    """Report non-regular/non-dir entries (FIFOs, sockets, devices) in run_dir; fail closed past limit."""
    import stat
    out, n = [], 0
    for base, dirs, files in os.walk(run_dir):
        for name in dirs + files:
            n += 1
            if n > limit:
                return out + [f"the run directory has more than {limit} entries: its files were not checked"]
            p = os.path.join(base, name)
            try:
                mode = os.stat(p).st_mode
            except OSError:
                continue
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                out.append(f"{os.path.relpath(p, run_dir)!r} in the run is not a regular file: remove it, or write "
                           "a plain file")
    return out


def _ledger_claims(run_dir: str) -> dict | None:
    lp = os.path.join(run_dir, "models", "claims_ledger.yaml")
    if not os.path.exists(lp):
        return None
    try:
        import render_claims as RCL
        return RCL.load_claims_tolerant(lp)[0]
    except Exception:  # noqa: BLE001 -- report_claim_findings reports the unreadable ledger
        return None


def _quoted(text, claims: dict | None):
    import render_claims as RCL
    return RCL.quote_refs(text, claims or {}, render=False) if isinstance(text, str) else text


def write_declared(run_dir: str, opened: list[dict], decision: dict) -> None:
    import yaml
    known = _ledger_claims(run_dir)
    _put(run_dir, DECLARED_FILE, yaml.safe_dump({
        "rule": decision.get("rule_matched"), "rationale": decision.get("rationale"),
        "declared": [{"id": o["id"], "severity": o["severity"], "source": o["source"],
                      "label": _quoted(short_label(o["claim"]), known), "claim": _quoted(o["claim"], known),
                      "required_remedy": _quoted(o.get("fix_requires"), known), "tested": bool(o.get("tested"))}
                     for o in opened]}, sort_keys=False, width=10**6))


def report_claim_findings(run_dir: str) -> list[dict]:
    """Unresolved [CLAIM:id], invalid ledger entries, or raw claim ids in manuscript.md/report.md."""
    import re
    rp, lp = os.path.join(run_dir, "manuscript.md"), os.path.join(run_dir, "models", "claims_ledger.yaml")
    if not (os.path.exists(rp) and os.path.exists(lp)):
        return []
    try:
        import render_claims as RCL
        claims, invalid = RCL.load_claims_tolerant(lp)
    except Exception as e:  # noqa: BLE001 -- fail closed
        return [{"kind": "report_claims_unreadable", "severity": "HIGH", "stage": "WRITE", "target_stage": "WRITE",
                 "key": "claims", "scope_declarable": False, "claim": f"claims ledger unreadable: {e}"}]
    text = RCL.normalise_refs(open(rp).read(), claims)
    out = [{"kind": "report_claim_invalid", "severity": "HIGH", "stage": "WRITE", "target_stage": "WRITE", "key": cid,
            "scope_declarable": False, "claim": f"The claims ledger's entry {cid} is invalid and cannot be rendered: {why}"}
           for cid, why in sorted(invalid.items())]
    refs = {c if "\n" in c else c.strip() for c in re.findall(r"\[CLAIM:([^\]]*)\]", text)}
    for cid in sorted(refs - set(claims) - set(invalid)):
        out.append({"kind": "report_claim_unresolved", "severity": "HIGH", "stage": "WRITE", "target_stage": "WRITE",
                    "key": cid, "scope_declarable": False,
                    "claim": f"The report has [CLAIM:{cid}], which is not in the claims ledger."})
    bare = re.sub(r"\[CLAIM:[^\]\n]*\]", " ", text)
    for cid in sorted(c for c in claims if "." in c or "_" in c):
        if re.search(r"(?<![A-Za-z0-9_.:\-])" + re.escape(cid) + r"(?![A-Za-z0-9_\-]|\.[A-Za-z0-9_])", bare):
            out.append({"kind": "report_claim_id_raw", "severity": "HIGH", "stage": "WRITE", "target_stage": "WRITE",
                        "key": cid, "scope_declarable": False,
                        "claim": f"The report shows the claim id {cid} instead of its value: write [CLAIM:{cid}]."})
    rp2 = os.path.join(run_dir, "report.md")
    if os.path.exists(rp2):
        left = [x for x in re.findall(r"\[CLAIM:([^\]]*)\]", open(rp2).read()) if x.strip() not in invalid]
        if left:
            out.append({"kind": "report_claim_unrendered", "severity": "HIGH", "stage": "WRITE", "target_stage": "WRITE",
                        "key": "unrendered", "scope_declarable": False,
                        "claim": f"The report still shows {len(left)} claim reference(s) ({', '.join(sorted(set(left))[:5])}), "
                                 "e.g. inside code; write [CLAIM:id] in plain text."})
    return out


def _status_line(run_dir: str, status: str | None) -> str:
    """Harness run-status banner stamped at the top of report.md."""
    if status is None:
        try:
            st = json.load(open(os.path.join(run_dir, STATE_FILE)))
        except (OSError, ValueError, TypeError):
            st = None
        if isinstance(st, dict):
            status = st.get("status") or {"declared": "completed_scope_declared",
                                          "not_accepted": "not_accepted"}.get(st.get("write_mode") or "", "pending")
        else:
            status = "pending"
    text = (_STATUS_LINES.get(status)
            or (_STATUS_LINES["pending"] if status == "pending" else _STATUS_LINES["other"]))
    return f"{STATUS_MARK}\n**Run status: {text}.** (`{status}`; rendered by the harness)\n\n"


def _lead_may_accept(item: dict, ledger: dict | None = None) -> bool:
    """Under --signoff lead, lead may provisionally accept harness/non-HIGH items, never open critic HIGH findings."""
    fid = item.get("finding")
    f = (ledger or {}).get(fid) if fid else None
    kind = item.get("kind") or (getattr(f, "kind", None) if f is not None else None)
    src = item.get("source") or (getattr(f, "source", None) if f is not None else None)
    is_critic = (kind == FL.CRITIC_KIND) or (isinstance(src, str) and src.startswith("critique"))
    if not is_critic:
        return True
    lsev = getattr(f, "severity", None) if f is not None else None
    isev = item.get("severity")
    if lsev is None and isev is None:
        return False
    if lsev is not None and not (isinstance(lsev, str) and lsev.upper() in FL.NON_HIGH_SEVERITIES):
        return False
    if isev is not None and not (isinstance(isev, str) and isev.upper() in FL.NON_HIGH_SEVERITIES):
        return False
    return True


def _plan_changing(item: dict) -> bool:
    conf = item.get("conflict")
    if item.get("kind") == "evidence_conflict_declared" and not isinstance(conf, dict):
        return True
    return item.get("kind") in VG.DECISION_BEARING_KINDS or (isinstance(conf, dict) and conf.get("changes_plan") is True) \
        or any((d.get("test") or {}).get("decision_changes") is True
               for d in item.get("declarations") or [] if isinstance(d, dict))


def provisional_lines(run_dir: str) -> tuple[list[str], list[str]]:
    """Lead's provisional decisions of a --signoff lead run (top banner lines, trailing report section)."""
    import scope_signoff as SS
    try:
        rows = [(r.get("finding"), bool(r.get("plan_changing")), (r.get("review") or {}).get("decision"))
                for r in SS.provisional(run_dir)]
    except Exception as e:  # noqa: BLE001 -- fail closed
        return ([f"**Provisional decisions: the list could not be read ({type(e).__name__}).**\n\n"],
                ["", "## Provisional decisions (the lead's)", "", f"Could not be read: {type(e).__name__}: {e}"])
    try:
        ch = json.load(open(os.path.join(run_dir, AB.ENGINE_CHOICE)))
        deps = [d for d in ch.get("engine_departures") or [] if isinstance(d, dict) and d.get("status") == "provisional_lead"]
    except (OSError, ValueError, AttributeError):
        deps = []
    try:
        changes = {str(t.get("id")) for t in (json.load(open(os.path.join(run_dir, AB.ANALYSIS_JSON))) or {}).get(
            "decision_tests") or [] if isinstance(t, dict) and t.get("plan_changes")}
    except (OSError, ValueError, AttributeError, TypeError):
        changes = set()
    rows += [(f"engine departure {d.get('benchmark') or d.get('component')}", str(d.get("decision_test")) in changes,
              None) for d in deps]
    if not rows:
        return [], []
    flagged = [f for f, pc, _r in rows if pc]
    how = (f"`python3 scripts/scope_signoff.py review {run_dir} --finding <id> --confirm|--overturn`; an engine departure "
           "is confirmed by the owner's signature of the spec in a relaunch")
    top = [f"**Provisional decisions that change the plan.** This run was unattended (`--signoff lead`); the lead "
           f"answered, provisionally, {len(flagged)} decision(s) that are the owner's and whose decision test changes the "
           f"plan: {', '.join(map(str, flagged))}. The owner confirms or overturns each ({how}).\n\n"] if flagged else []
    sec = ["", "## Provisional decisions (the lead's, under --signoff lead; for the owner's review)", "",
           f"Each decision below is the owner's; the lead made it provisionally so the run could finish. Review: {how}.", ""]
    untested = [f for f, _pc, _r in rows if str(f).startswith(tuple(f"{k}:" for k in FL.NOT_RUN_KINDS))]
    if untested:
        top.append(f"**Provisional decisions on decision tests that did not run.** The lead accepted, provisionally, "
                   f"going on without {len(untested)} declared decision test(s) that did not run: "
                   f"{', '.join(map(str, untested))}. The plan was not tested against them. The owner confirms or "
                   f"overturns each ({how}).\n\n")
    sec += [f"- **{f}**: accepted provisionally; plan changes: "
            f"{'not tested (the decision test did not run)' if f in untested else 'yes' if pc else 'no'}; owner review: "
            f"{r or 'not yet'}." for f, pc, r in rows]
    return top, sec


def dependence_lines(run_dir: str, stale_out: list | None = None) -> list[str]:
    """Dependence-on-accepted-decision-test summary block for the top of report.md."""
    try:
        led = FL.load(run_dir)
    except Exception as e:  # noqa: BLE001 -- fail closed
        return [f"**Accepted dependence of the plan: the list could not be read ({type(e).__name__}).**\n\n"]
    rows = sorted((f for f in led.values() if f.kind in VG.DECISION_BEARING_KINDS and f.status == "owner_accepted"),
                  key=lambda f: f.id)
    if not rows:
        return []
    from toolkit.analysis import contract as _CT
    import yaml
    try:
        ref = (_CT._verified_doc(run_dir)[3] or {}).get("reference_plan")
    except Exception:  # noqa: BLE001
        ref = None
    evs = {f.id: next((e for e in reversed(f.events or []) if isinstance(e, dict)
                       and e.get("status") == "owner_accepted"), {}) for f in rows}
    by_lead = all(e.get("by") == "lead" for e in evs.values())
    whose = ("The lead accepted the plan with that dependence, provisionally, under `--signoff lead`; the owner confirms "
             "or overturns each" if by_lead else "Each was accepted as its line says")
    out = ["## The plan depends on inputs the owner accepted\n\n" if not by_lead else
           "## The plan depends on inputs the lead accepted provisionally\n\n",
           "Each decision test below re-solves the plan over the range it tests and changes it there; each line says "
           f"which plan its units and feasibility are counted against. {whose} (harness-rendered).\n\n"]
    for f in rows:
        ev = evs[f.id]
        who = ("the lead, provisionally (`--signoff lead`; the owner confirms or overturns it)" if ev.get("by") == "lead"
               else "the owner")
        line = f"- **{f.key or f.id}**: accepted by {who}" + (f" at {ev['at']}" if ev.get("at") else "")
        v_now = {"kind": f.kind, "key": f.key, "claim": f.claim, "source": "toolkit/analysis/contract.py"}
        stale_line = (line + "; its record has changed since it was accepted, so the gate asks again and the values "
                             "are not repeated here.\n")
        bare_line = line + "; no verified values are recorded for it, so none are shown.\n"

        def stale():
            out.append(stale_line)
            if stale_out is not None:
                stale_out.append(f.id)

        try:
            rec = VG._bearing_record(run_dir, v_now)
            same = bool(ev.get("fingerprint")) and VG._bearing_fp(v_now, rec) == ev["fingerprint"]
        except Exception:  # noqa: BLE001
            rec, same = None, False
        if not same:
            stale()
            continue
        if not isinstance(rec, dict):
            out.append(bare_line)
            continue
        res, changed = None, False
        try:
            if rec.get("path") and rec.get("result_sha256") and rec.get("result_sha256") == ev.get("result_sha256"):
                raw = open(os.path.join(run_dir, str(rec["path"])), "rb").read()
                if hashlib.sha256(raw).hexdigest() == ev["result_sha256"]:
                    res = yaml.safe_load(raw.decode("utf-8")) or {}
                else:
                    changed = True
        except Exception:  # noqa: BLE001
            res = None
        if changed:
            stale()
            continue
        if not isinstance(res, dict):
            out.append(bare_line)
            continue
        what = str(res.get("varied") or res.get("question") or "the input this test varies")
        against = ("the point-value plan (the delivered plan is the expected-value plan over the uncertainty draws, "
                   "which the test does not re-solve)") if ref == "point_plan" else "the delivered plan"
        vals = []
        for r in res.get("results") or []:
            if not isinstance(r, dict) or not r.get("decision_changes"):
                continue
            ids = r.get("units_changed_ids")
            names = f" ({', '.join(map(str, ids))})" if isinstance(ids, list) and ids else ""
            cm = r.get("cost_multipliers")
            at = ", ".join(f"{k} x{v}" for k, v in cm.items()) if isinstance(cm, dict) and cm else r.get("value")
            vals.append(f"at {at}: {r.get('units_changed')} unit(s) change package{names}"
                        + (f"; re-solved plan {r['recommendation']}" if r.get("recommendation") else "")
                        + ("; the reference plan is not feasible there" if r.get("point_plan_feasible") is False else "")
                        + ("; not settled within the solver's gap" if r.get("within_solver_gap") else ""))
        out.append(f"{line}; varies {what}; counted against {against}"
                   + (". " + "; ".join(vals) + ".\n" if vals else ".\n"))
    return out + ["\n"]


def amendment_lines(run_dir: str) -> list[str]:
    """Provisional-amendment disclaimer when a manual amendment record exists and owner has not signed."""
    p = os.path.join(run_dir, AMENDMENT_RECORD)
    try:
        st = json.load(open(os.path.join(run_dir, STATE_FILE)))
    except (OSError, ValueError):
        st = None
    hist = st.get("history") if isinstance(st, dict) and isinstance(st.get("history"), list) else []
    s7 = next((h for h in hist if isinstance(h, dict) and h.get("provisional_amendment")), None)
    if not os.path.lexists(p) and s7 is None:
        return []
    try:
        from toolkit.spec import signoff as SO
        so = SO._spec_record(run_dir) if SO.status(run_dir) == "signed" else None
        by = (so or {}).get("signed_by") or (so or {}).get("by")
    except Exception:  # noqa: BLE001
        by = None
    if by == "owner":
        return []
    try:
        rec = json.load(open(p))
        ch = "; ".join(f"{c.get('op')} `{c.get('path')}`" + ("" if c.get("op") == "remove" else
                                                             f" to {json.dumps(c.get('after'), default=str)[:160]}")
                       for c in rec.get("changes") or [] if isinstance(c, dict))
        parent = str((rec.get("parent_run") or {}).get("run_dir") or "")
    except (OSError, ValueError, AttributeError, TypeError) as e:
        return [f"**Provisional amendment of the signed spec ({(s7 or {}).get('provisional_amendment') or 'unnamed'}): "
                f"its record could not be read ({type(e).__name__}); the owner has not reviewed it.**\n\n"]
    if s7 is not None:
        how = "which the lead made from that run's rethink request and signed provisionally"
        cmd = (f"the owner confirms it by amending the parent with the same patch and signing that child "
               f"(`python3 -m toolkit.spec.amend {shlex.quote(parent)} --from-request --signoff owner`), or "
               "overturns it by amending the parent differently or not at all")
    else:
        how = "made with toolkit/spec/amend.py and not signed by the owner"
        cmd = "the owner confirms it by amending the parent with the same patch file and signing that child"
    return [f"**Provisional amendment of the signed spec.** This run continues `{parent}` under amendment "
            f"`{rec.get('id')}`, {how} (signed by {by or 'nobody yet'}); the owner has not reviewed it. Reason: "
            f"{' '.join(str(rec.get('reason')).split())[:500]} Changes: {ch or 'none recorded'}. {cmd[0].upper() + cmd[1:]}."
            "\n\n"]


def _write_deliverable(run_dir: str) -> None:
    """Write the spec's `deliverable_file` from the allocation CSV when valid."""
    import csv
    import io
    import yaml
    from lib.safe_io import read_nofollow
    try:
        spec = yaml.safe_load(read_nofollow(run_dir, AB.SPEC_FILE)[0]) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError, RecursionError):
        return
    if not isinstance(spec, dict):
        return
    df = spec.get("deliverable_file")
    if not isinstance(df, dict) or deliverable_problems(run_dir):
        return
    try:
        raw_csv = read_nofollow(run_dir, AB.ALLOCATION_CSV)[0].decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return
    reader = csv.DictReader(io.StringIO(raw_csv))
    cols = df["columns"]
    if not reader.fieldnames or not all(isinstance(c, str) and c in reader.fieldnames for c in cols.values()):
        return
    names = df.get("package_names") if isinstance(df.get("package_names"), dict) else {}
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(list(cols))
    for r in reader:
        w.writerow([names.get(r.get(c, ""), r.get(c, "")) if c == "package" else r.get(c, "")
                    for c in cols.values()])
    _put(run_dir, str(df["path"]), buf.getvalue())


def render_report(run_dir: str, backend=None, status: str | None = None) -> str:
    """Render report.md from manuscript.md, [CLAIM:id] values, headline tables, scope/declared/evidence blocks."""
    import yaml
    import render_claims as RCL
    backend = backend or AB.backend("toolkit")
    draft = open(os.path.join(run_dir, "manuscript.md")).read()
    known = _ledger_claims(run_dir)
    if known is not None:
        try:
            draft, _unresolved = RCL.render_text(RCL.normalise_refs(draft, known), known)
        except Exception:  # noqa: BLE001 -- left unrendered; report_claim_findings flags it
            pass
    try:
        h = backend.headline(run_dir)
    except FileNotFoundError:
        if any(os.path.lexists(os.path.join(run_dir, "analysis", f)) for f in ("point.yaml", "estimates.yaml")):
            raise
        h = {}
    lines = ["", "<!-- harness: rendered from analysis/analysis.json -->", "## Numbers (rendered by the harness)", "",
             "| quantity | value |", "|---|---|"]
    lines += [f"| {k} | {v:,.4g} |" if isinstance(v, (int, float)) else f"| {k} | {v} |" for k, v in h.items()]
    try:
        from toolkit.spec import engine_parameters as PV
        sv = PV.report_block(run_dir)
    except Exception as e:  # noqa: BLE001
        sv = ["## Engine parameter values set by the signed spec", "",
              f"Could not be rendered from analysis/engine.yaml: {type(e).__name__}: {e}", ""]
    if sv:
        lines += [""] + sv[:-1]
    decl, sd_find = [], []
    if os.path.exists(os.path.join(run_dir, "scope_declaration.yaml")):
        try:
            from lib import scope_declarations as SD
            _doc, decl, sd_find = SD.load(run_dir)
        except Exception as e:  # noqa: BLE001
            SD, sd_find = None, [{"claim": f"{type(e).__name__}: {e}"}]
    if decl or sd_find:
        lines += ["", "## Declared limits (rendered in full from scope_declaration.yaml)", ""]
        lines += [f"- scope_declaration.yaml could not be read: {f.get('claim')}" for f in sd_find]
        for e in decl:
            ids = [str(x) for k in SD.STRUCTURED_ID_FIELDS
                   for x in (e.get(k) if isinstance(e.get(k), list) else [e.get(k)]) if x]
            text = " ".join(f"{f.replace('_', ' ').capitalize()}: {' '.join(str(e[f]).split())}"
                            for f in SD.JUSTIFICATION_FIELDS if e.get(f))
            fix = _remedy(e)
            lines.append(f"- **{e.get('id') or 'unnamed'}** ({', '.join(ids) or 'no finding named'}): "
                         f"{text or 'no text given.'}" + (f" Required remedy: {fix}" if fix else ""))
    try:
        dec2 = (yaml.safe_load(open(os.path.join(run_dir, DECLARED_FILE))) or {})
    except OSError:
        dec2 = {}
    if dec2.get("declared"):
        lines += ["", "## Open findings, declared (not resolved)", "", str(dec2.get("rationale") or ""), ""]
        lines += [f"- **{e['id']}** ({e['severity']}, {e['source']}): {e['claim']} Required remedy: "
                  f"{e.get('required_remedy') or 'not stated by the finding.'}"
                  for e in dec2["declared"]]
    try:
        from toolkit.spec import evidence_draft as EDR
        so = {}
        for f in (SIGNOFF_FILE, "decision_brief.signoff.json"):
            try:
                d_ = json.load(open(os.path.join(run_dir, f)))
            except (OSError, ValueError):
                continue
            binds = isinstance(d_, dict) and ((d_.get("sha256") or {}).get(AB.SPEC_FILE) if isinstance(
                d_.get("sha256"), dict) else None) or (isinstance(d_, dict) and d_.get("spec_sha256"))
            if binds and (d_.get("signed_by") or d_.get("by")):
                so = d_
                break
        eb = EDR.report_block(run_dir, so.get("signed_by") or so.get("by"))
    except Exception as e:  # noqa: BLE001
        eb = ["## Evidence dossier", "", f"Could not be rendered from the evidence record: {type(e).__name__}.", ""]
    if eb:
        lines += [""] + eb[:-1]
    from toolkit.analysis import conflicts as CF
    cl = CF.report_lines(run_dir)
    if cl:
        lines += [""] + cl[:-1]
    top, prov = provisional_lines(run_dir)
    lines += prov
    stale_dt: list = []
    from toolkit.engines.built import signoff as _BSIG
    be_disc = _BSIG.report_disclaimer_lines(run_dir)
    head = (_status_line(run_dir, status) + "".join(be_disc) + "".join(amendment_lines(run_dir)) + "".join(top)
            + "".join(dependence_lines(run_dir, stale_dt)))
    body = draft.rstrip() + "\n"
    head, tail = RCL.quote_refs(head, known or {}), RCL.quote_refs("\n".join(lines), known or {})
    full = head + body + tail + "\n"
    _put(run_dir, "report.md", full)
    _write_json(run_dir, "report_render.json", {"report_sha256": hashlib.sha256(full.encode("utf-8")).hexdigest(),
                                                "writer_span": [len(head), len(head) + len(body)],
                                                "dependence_stale": sorted(stale_dt)})
    _write_deliverable(run_dir)
    return os.path.join(run_dir, "report.md")


def _render_stale(run_dir: str) -> list:
    """Accepted decision tests the last rendering could not state as accepted; unreadable fails closed."""
    try:
        doc = json.load(open(os.path.join(run_dir, "report_render.json")))
        st = doc.get("dependence_stale") if isinstance(doc, dict) else None
        return list(st) if isinstance(st, list) else ["report_render.json"]
    except (OSError, ValueError):
        return ["report_render.json"]


def _self_test() -> int:
    import tempfile
    import yaml

    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    with tempfile.TemporaryDirectory() as td:
        for st, expect in (
            ("completed", "accepted by the final gate"),
            ("completed_scope_declared", "declared scope, not accepted"),
            ("pending", "not yet decided by the final gate"),
            ("run_failed", "not accepted"),
        ):
            line = _status_line(td, st)
            ok(f"**Run status: {expect}.**" in line and f"`{st}`" in line,
               f"RR1 _status_line formats {st!r} cleanly ({line.strip()!r})")
        ok("not yet decided by the final gate" in _status_line(td, None),
           "RR1b _status_line defaults cleanly when run_state.json is absent")

        open(os.path.join(td, "report.md"), "w").write("draft\n")
        _withhold_report(td)
        ok(not os.path.exists(os.path.join(td, "report.md"))
           and os.path.exists(os.path.join(td, "report_withheld.md")),
           "RR2 _withhold_report moves report.md to report_withheld.md")
        open(os.path.join(td, "report.md"), "w").write("final\n")
        _retire_stale_withheld(td)
        ok(not os.path.exists(os.path.join(td, "report_withheld.md")),
           "RR3 _retire_stale_withheld removes report_withheld.md once report.md is delivered")

        d_del = os.path.join(td, "deliv")
        os.makedirs(d_del)
        yaml.safe_dump({"deliverable_file": {"path": "report.md", "columns": {"u": "unit"}}},
                       open(os.path.join(d_del, AB.SPEC_FILE), "w"))
        probs = deliverable_problems(d_del)
        ok(any("owns" in p for p in probs), f"RR4 deliverable_problems refuses harness-owned targets ({probs})")

        d_rep = os.path.join(td, "rep")
        os.makedirs(os.path.join(d_rep, "models"), exist_ok=True)
        os.makedirs(os.path.join(d_rep, "analysis"), exist_ok=True)
        yaml.safe_dump({"claims": [{"id": "c.val", "claim_kind": "scalar", "value": 42.0, "unit": "cases"}]},
                       open(os.path.join(d_rep, "models", "claims_ledger.yaml"), "w"))
        open(os.path.join(d_rep, "manuscript.md"), "w").write("Observed [CLAIM:c.val] and bad [CLAIM:no.such].\n")
        write_declared(d_rep, [{"id": "R-001", "severity": "MEDIUM", "source": "critique-redteam",
                                "claim": "c", "fix_requires": "f", "tested": False}],
                       {"rule_matched": "3m", "rationale": "none"})
        fake_b = type("_B", (), {"headline": staticmethod(lambda _rd: {"mean": 1.25})})()
        rep_p = render_report(d_rep, fake_b, status="completed_scope_declared")
        rep_txt = open(rep_p).read()
        rc_probs = report_claim_findings(d_rep)
        ok("42" in rep_txt and "**Run status: declared scope, not accepted.**" in rep_txt
           and "## Open findings, declared" in rep_txt
           and any(f["kind"] == "report_claim_unresolved" for f in rc_probs),
           f"RR5 render_report without decision_spec.yaml renders claims, status banner, declared findings, and flags unresolved refs ({rc_probs})")

        yaml.safe_dump({"deliverable_file": {"path": "deliverable.csv",
                                             "columns": {"unit": "unit", "package": "package"},
                                             "package_names": {"p1": "Package One"}}},
                       open(os.path.join(d_rep, AB.SPEC_FILE), "w"), sort_keys=False)
        open(os.path.join(d_rep, AB.ALLOCATION_CSV), "w").write("unit,package\nu1,p1\n")
        render_report(d_rep, fake_b, status="completed")
        deliv_p = os.path.join(d_rep, "deliverable.csv")
        ok(os.path.isfile(deliv_p) and "u1,Package One" in open(deliv_p).read(),
           f"RR6 render_report with decision_spec.yaml writes deliverable_file cleanly")

        for idx, raw_spec in enumerate(("- item\n", '"scalar"\n', "42\n", "a: [unclosed\n")):
            d_edge = os.path.join(td, f"edge_spec_{idx}")
            os.makedirs(os.path.join(d_edge, "analysis"), exist_ok=True)
            open(os.path.join(d_edge, AB.SPEC_FILE), "w").write(raw_spec)
            open(os.path.join(d_edge, AB.ALLOCATION_CSV), "w").write("unit,package\nu1,p1\n")
            ok(deliverable_problems(d_edge) == [], f"RR7a deliverable_problems tolerates non-dict/bad YAML #{idx}")
            _write_deliverable(d_edge)

        d_sym = os.path.join(td, "edge_sym")
        os.makedirs(os.path.join(d_sym, "analysis"), exist_ok=True)
        sym_target = os.path.join(td, "outside_spec.yaml")
        yaml.safe_dump({"deliverable_file": {"path": "deliverable.csv", "columns": {"unit": "unit"}}},
                       open(sym_target, "w"), sort_keys=False)
        os.symlink(sym_target, os.path.join(d_sym, AB.SPEC_FILE))
        open(os.path.join(d_sym, AB.ALLOCATION_CSV), "w").write("unit,package\nu1,p1\n")
        ok(deliverable_problems(d_sym) == [], "RR7b deliverable_problems ignores symlinked spec")
        _write_deliverable(d_sym)
        ok(not os.path.exists(os.path.join(d_sym, "deliverable.csv")),
           "RR7b _write_deliverable does not follow symlinked decision_spec.yaml")

        d_pn = os.path.join(td, "edge_pkg_names")
        os.makedirs(os.path.join(d_pn, "analysis"), exist_ok=True)
        yaml.safe_dump({"deliverable_file": {"path": "deliverable.csv",
                                             "columns": {"unit": "unit", "package": "package"},
                                             "package_names": ["p1"]}},
                       open(os.path.join(d_pn, AB.SPEC_FILE), "w"), sort_keys=False)
        open(os.path.join(d_pn, AB.ALLOCATION_CSV), "w").write("unit,package\nu1,p1\n")
        _write_deliverable(d_pn)
        ok("u1,p1" in open(os.path.join(d_pn, "deliverable.csv")).read(),
           "RR7c _write_deliverable handles non-dict package_names without crashing")

        d_mc = os.path.join(td, "edge_missing_col")
        os.makedirs(os.path.join(d_mc, "analysis"), exist_ok=True)
        yaml.safe_dump({"deliverable_file": {"path": "deliverable.csv",
                                             "columns": {"unit": "no_such_col"}}},
                       open(os.path.join(d_mc, AB.SPEC_FILE), "w"), sort_keys=False)
        open(os.path.join(d_mc, AB.ALLOCATION_CSV), "w").write("unit,package\nu1,p1\n")
        _write_deliverable(d_mc)
        ok(not os.path.exists(os.path.join(d_mc, "deliverable.csv")),
           "RR7d _write_deliverable skips cleanly when deliverable column is absent from allocation.csv")

        for tag, bad_rel, mk_dir in (
            ("trailing_slash", "out.csv/", False),
            ("existing_dir", "existing_dir", True),
        ):
            d_bad = os.path.join(td, f"edge_{tag}")
            os.makedirs(os.path.join(d_bad, "analysis"), exist_ok=True)
            if mk_dir:
                os.makedirs(os.path.join(d_bad, bad_rel), exist_ok=True)
            yaml.safe_dump({"deliverable_file": {"path": bad_rel, "columns": {"u": "unit"}}},
                           open(os.path.join(d_bad, AB.SPEC_FILE), "w"), sort_keys=False)
            open(os.path.join(d_bad, AB.ALLOCATION_CSV), "w").write("unit,package\nu1,p1\n")
            probs = deliverable_problems(d_bad)
            _write_deliverable(d_bad)
            ok(bool(probs), f"RR7e deliverable_problems rejects {tag} ({probs}) and _write_deliverable does not crash")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test() if sys.argv[1:2] == ["--self-test"] else 2)
