"""Harness findings on the deliverable and the report (owned paths, claims, acceptance language).

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import json
import os
import analysis_backend as AB
import findings_ledger as FL
import fix_loop as FX

from .common import PROTECTED_BY_FIX, SCOPE_SIGNOFF, SIGNOFF_FILE, SIGNOFF_SHEET, STATE_FILE
from .amend import AMENDMENT_RECORD


def deliverable_problems(run_dir: str) -> list[str]:
    """The spec's `deliverable_file.path` must be a plain file inside the run directory: relative,
    no `..`, no symlink on the way, and not a file the harness, the gate or a critic owns."""
    import yaml
    try:
        spec = yaml.safe_load(open(os.path.join(run_dir, AB.SPEC_FILE))) or {}
    except OSError:
        return []
    df = spec.get("deliverable_file")
    if df is None:
        return []
    rel = df.get("path") if isinstance(df, dict) else None
    if not isinstance(rel, str) or not rel.strip() or not isinstance(df.get("columns"), dict):
        return ["deliverable_file needs a path and columns"]
    if os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
        return [f"deliverable path {rel!r} must be relative and inside the run directory"]
    root = os.path.realpath(run_dir)
    target = os.path.join(root, rel)
    if not os.path.realpath(target).startswith(root + os.sep):
        return [f"deliverable path {rel!r} resolves outside the run directory"]
    cur = root
    for part in rel.replace("\\", "/").split("/"):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return [f"deliverable path {rel!r} goes through a symlink ({os.path.relpath(cur, root)})"]
    norm = os.path.relpath(target, root).replace(os.sep, "/")
    # v2 autonomy 1B-ii: the signed evidence dossier the spec names, compared as a literal path (Codex r2 P1-1: a
    # name such as x[1].yaml is not its own glob) and by identity
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


def harness_owned() -> tuple:
    """The run files the harness, the gate or a critic owns (glob patterns): no deliverable and no supplied evidence
    dossier may be one of them."""
    import pipeline as P
    return tuple(P._reserved_patterns()) + PROTECTED_BY_FIX + (
        STATE_FILE, SIGNOFF_FILE, SCOPE_SIGNOFF, "spec_rethink_request.json", FL.LEDGER, FX.IMPACT, "report.md", "manuscript.md", "metadata.json",
        "report_render.json", "report_withheld.md", "final_gate.json", AB.ENGINE_CHOICE, "validator_history.jsonl", "gate_r*", "fix_plan_r*", "fix_progress_r*",
        "analysis/*", "models/pipeline*", "evidence/*", "data/*", "world_model", "world_model/*",
        "evidence_dossier.yaml", SIGNOFF_SHEET,    # v2 autonomy 1B-ii: the default dossier and the sign-off sheet
        AMENDMENT_RECORD)                          # lead-gaps β (Codex r1): toolkit/spec/amend.py's record


def owned_path(root: str, rel: str, owned) -> bool:
    """Is `rel` (inside `root`) one of the owned paths? Compared case-insensitively (APFS and NTFS
    are case-insensitive by default: Report.MD is report.md), and, when the file exists, by
    identity: the same inode as an existing owned file, or any hard link (st_nlink > 1)."""
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
        return True                  # fail closed
    return False


# ---- claims in the write-up
def report_claim_findings(run_dir: str) -> list[dict]:
    """The report must show values, never claim ids: an unresolved [CLAIM:id], or a claims-ledger id
    left bare in the text the writer wrote, is a HIGH write-up finding (not declarable)."""
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
    # The writer's own text, all of it: what the harness appends is not in manuscript.md, so no
    # marker the writer could copy can hide anything.
    text = open(rp).read()
    # One invalid claim is its own finding (the valid ones still render: render_report); still HIGH and not
    # declarable, as the whole-ledger refusal it replaces was.
    out = [{"kind": "report_claim_invalid", "severity": "HIGH", "stage": "WRITE", "target_stage": "WRITE", "key": cid,
            "scope_declarable": False, "claim": f"The claims ledger's entry {cid} is invalid and cannot be rendered: {why}"}
           for cid, why in sorted(invalid.items())]
    # A reference is read as the renderer reads it (render_claims.render_text strips the id).
    refs = {c if "\n" in c else c.strip() for c in re.findall(r"\[CLAIM:([^\]]*)\]", text)}   # no id spans lines
    for cid in sorted(refs - set(claims) - set(invalid)):
        out.append({"kind": "report_claim_unresolved", "severity": "HIGH", "stage": "WRITE", "target_stage": "WRITE",
                    "key": cid, "scope_declarable": False,
                    "claim": f"The report has [CLAIM:{cid}], which is not in the claims ledger."})
    bare = re.sub(r"\[CLAIM:[^\]\n]*\]", " ", text)       # an id inside a reference is not a bare id
    for cid in sorted(c for c in claims if "." in c or "_" in c):     # a plain word is not an id
        if re.search(r"(?<![A-Za-z0-9_.:\-])" + re.escape(cid) + r"(?![A-Za-z0-9_\-]|\.[A-Za-z0-9_])", bare):
            out.append({"kind": "report_claim_id_raw", "severity": "HIGH", "stage": "WRITE", "target_stage": "WRITE",
                        "key": cid, "scope_declarable": False,
                        "claim": f"The report shows the claim id {cid} instead of its value: write [CLAIM:{cid}]."})
    typed = typed_numbers(text, given_values(run_dir, claims))
    rp2 = os.path.join(run_dir, "report.md")
    if os.path.exists(rp2):
        left = re.findall(r"\[CLAIM:([^\]]*)\]", open(rp2).read())
        if left:
            out.append({"kind": "report_claim_unrendered", "severity": "HIGH", "stage": "WRITE", "target_stage": "WRITE",
                        "key": "unrendered", "scope_declarable": False,
                        "claim": f"The report still shows {len(left)} claim reference(s) ({', '.join(sorted(set(left))[:5])}), "
                                 "e.g. inside code; write [CLAIM:id] in plain text."})
    if typed:
        # A text heuristic with a measured false-positive rate of 45% or more (design note §13): LOW,
        # advisory. It is reported (findings.jsonl, the presentation critic's input) but never blocks,
        # goes to the fix loop or is declared. It goes back up only after structural exemptions for its
        # false-positive classes and a new measurement. The structural cases above stay HIGH.
        out.append({"kind": "report_number_typed", "severity": "LOW", "stage": "WRITE", "target_stage": "WRITE",
                    "key": "numbers", "scope_declarable": True,
                    "claim": f"The writer typed {len(typed)} number(s) ({', '.join(typed[:8])}) that are not in the spec, "
                             "the claims ledger or the question; write each as a [CLAIM:id] the harness renders."})
    return out


def report_acceptance_findings(run_dir: str, write_mode: str | None) -> list[dict]:
    """F6 S1-7: a declared-scope or not-accepted write-up uses no acceptance language (the terms are in
    .claude/orchestration/acceptance_language.yaml; the check is scripts/writer_qa.py's). MEDIUM: a
    write-up fix the loop can make, declared when it cannot."""
    if write_mode not in ("declared", "not_accepted"):
        return []
    try:
        text = open(os.path.join(run_dir, "manuscript.md")).read()
    except OSError:
        return []
    try:
        import writer_qa as WQ
        hits = WQ.acceptance_language_hits(text, "completed_scope_declared" if write_mode == "declared" else "not_accepted")
    except Exception as e:  # noqa: BLE001 -- fail closed
        hits = [f"the check could not run ({type(e).__name__}: {e})"]
    if not hits:
        return []
    return [{"kind": "report_acceptance_language", "severity": "MEDIUM", "stage": "WRITE", "target_stage": "WRITE",
             "key": "acceptance", "scope_declarable": True,
             "claim": (f"The write-up is {'declared scope' if write_mode == 'declared' else 'not accepted'} but uses "
                       f"acceptance language ({len(hits)}): " + "; ".join(hits)
                       + ". Say what was tested and what is declared.")}]


_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9, "bn": 1e9}


def given_values(run_dir: str, claims: dict | None = None) -> list[float]:
    """Numbers a report may show as typed: those in the signed spec and the question (the owner's
    facts), and every number in the claims ledger, the interval level included (0.95 is also 95)."""
    import re
    texts = []
    try:
        texts.append(open(os.path.join(run_dir, AB.SPEC_FILE)).read())
    except OSError:
        pass
    try:   # the question only: not timestamps or other run metadata
        texts.append(str(json.load(open(os.path.join(run_dir, "metadata.json"))).get("question") or ""))
    except (OSError, ValueError):
        pass
    vals = []
    for t in texts:
        vals += [float(g.replace(",", "")) for g in re.findall(r"\d[\d,]*(?:\.\d+)?", t)]

    def walk(x):
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float)):
            vals.append(float(x))
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
    walk(list((claims or {}).values()))
    vals += [v * 100 for v in list(vals) if 0 < abs(v) <= 1]          # proportions written as percentages
    return vals


def typed_numbers(text: str, given: list[float]) -> list[str]:
    """Numbers typed in `text` outside [CLAIM:] references that match none of `given` at the precision
    they are written with. Not years or year ranges, list or heading numbering, or digits inside an
    identifier (D09, DT1, R-004)."""
    import math
    import re
    bare = re.sub(r"\[CLAIM:[^\]]*\]", " ", text)
    bare = re.sub(r"(?m)^\s*(#+\s*)?\d+[.)]\s", " ", bare)
    num = re.compile(r"(?<![A-Za-z0-9_.])[-+\u2212]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?(?:\s?(?:[kKmMbB]n?|%))?(?![A-Za-z0-9_])")
    out = []
    for m_ in num.finditer(bare):
        t = m_.group(0).rstrip(",")
        before = bare[max(0, m_.start() - 1):m_.start()]
        if t[0] in "-+\u2212" and before and (before.isalnum() or before == "_"):
            continue                                  # an identifier such as R-004
        mant = re.match(r"[-+\u2212]?([\d,]*(?:\.\d+)?)", t).group(1).replace(",", "")
        rest = t[len(re.match(r"[-+\u2212]?[\d,]*(?:\.\d+)?", t).group(0)):].strip()
        if re.fullmatch(r"(19|20)\d\d", mant) and not rest:
            continue                                  # a year, or one end of a year range
        try:
            v = float(mant)
        except ValueError:
            continue
        exp = re.match(r"[eE]([-+]?\d+)", rest)
        if exp:
            v *= 10 ** int(exp.group(1))
            rest = rest[exp.end():].strip()
        mult = _SUFFIX.get(rest.lower(), 1.0)
        v *= mult
        digits = mant.replace(".", "").lstrip("0") or "0"
        sig = max(1, len(digits))
        def same(g):
            if g == 0 or v == 0:
                return g == v
            return float(f"{abs(g):.{sig}g}") == round(abs(v), 12) or math.isclose(abs(g), abs(v), rel_tol=1e-9)
        if any(same(g) for g in given):
            continue
        out.append(t)
    return out
