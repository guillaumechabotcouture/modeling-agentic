"""report.md: the harness's status line, provisional decisions and the deliverable.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import analysis_backend as AB

from .common import DECLARED_FILE, SIGNOFF_FILE, _put, _remedy, _write_json, load_state
from .amend import AMENDMENT_RECORD, _parent_of
from .findings import deliverable_problems


STATUS_MARK = "<!-- harness: run status -->"


def _status_line(run_dir: str, status: str | None) -> str:
    """F6 S1-7: the run status, from .claude/orchestration/acceptance_language.yaml (a declared-scope
    run reads "declared scope, not accepted"). Without a status: the run state's, else the write mode's."""
    if status is None:
        try:
            st = load_state(run_dir)
        except (OSError, ValueError, TypeError):
            st = None
        status = (st.status or {"declared": "completed_scope_declared", "not_accepted": "not_accepted"}.get(
            st.write_mode or "", "pending")) if st else "pending"
    try:
        import writer_qa as WQ
        lines = WQ.load_acceptance_language()["status_lines"]
    except Exception:  # noqa: BLE001 -- fail closed: never reads as accepted
        lines = {}
    text = lines.get(status) or (lines.get("pending") if status == "pending" else lines.get("other")) or "not accepted"
    return f"{STATUS_MARK}\n**Run status: {text}.** (`{status}`; rendered by the harness)\n\n"


def provisional_lines(run_dir: str) -> tuple[list[str], list[str]]:
    """(lines above the draft, a section after it): the lead's provisional decisions of a --signoff lead run (D2),
    the owner items answered (scripts/scope_signoff.py provisional) and the engine departures credited
    (`provisional_lead` in engine_choice.json). Those whose decision test changes the plan head the report."""
    import scope_signoff as SS
    try:
        rows = [(r.get("finding"), bool(r.get("plan_changing")), (r.get("review") or {}).get("decision"))
                for r in SS.provisional(run_dir)]
    except Exception as e:  # noqa: BLE001 -- fail closed: the report says the list could not be read
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
    sec += [f"- **{f}**: accepted provisionally; plan changes: {'yes' if pc else 'no'}; owner review: "
            f"{r or 'not yet'}." for f, pc, r in rows]
    return top, sec


def amendment_lines(run_dir: str) -> list[str]:
    """lead-gaps β: a run made by an amendment its owner has not signed (the lead's, provisional) heads its report with
    the amendment: its id, reason and changes, from toolkit/spec/amend.py's record. Nothing once the owner signs. The
    S7 handoff event in run_state.json keeps the heading when the record is lost (Codex r1) and selects the wording."""
    p = os.path.join(run_dir, AMENDMENT_RECORD)
    try:
        st = load_state(run_dir)
    except (OSError, ValueError):
        st = None
    s7 = next((h for h in (st.history if st else []) if isinstance(h, dict) and h.get("provisional_amendment")), None)
    if not os.path.lexists(p) and s7 is None:
        return []
    try:
        from toolkit.spec import signoff as SO
        so = SO._spec_record(run_dir) if SO.status(run_dir) == "signed" else None
        by = (so or {}).get("signed_by") or (so or {}).get("by")
    except Exception:  # noqa: BLE001 -- unknown signer: listed as provisional
        by = None
    if by == "owner":
        return []
    try:
        rec = json.load(open(p))
        ch = "; ".join(f"{c.get('op')} `{c.get('path')}`" + ("" if c.get("op") == "remove" else
                                                             f" to {json.dumps(c.get('after'), default=str)[:160]}")
                       for c in rec.get("changes") or [] if isinstance(c, dict))
        parent = _parent_of(run_dir, (rec.get("parent_run") or {}).get("run_dir"))
    except (OSError, ValueError, AttributeError, TypeError) as e:
        return [f"**Provisional amendment of the signed spec ({(s7 or {}).get('provisional_amendment') or 'unnamed'}): "
                f"its record could not be read ({type(e).__name__}); the owner has not reviewed it.**\n\n"]
    if s7 is not None:
        how = "which the lead made from that run's rethink request and signed provisionally"
        cmd = (f"the owner confirms it by amending the parent with the same patch and signing that child "
               f"(`python3 -m toolkit.spec.amend {shlex.quote(str(parent))} --from-request --signoff owner`), or "
               "overturns it by amending the parent differently or not at all")
    else:          # a manual amendment (amend --patch) the lead signed
        how = "made with toolkit/spec/amend.py and not signed by the owner"
        cmd = "the owner confirms it by amending the parent with the same patch file and signing that child"
    return [f"**Provisional amendment of the signed spec.** This run continues `{parent}` under amendment "
            f"`{rec.get('id')}`, {how} (signed by {by or 'nobody yet'}); the owner has not reviewed it. Reason: "
            f"{' '.join(str(rec.get('reason')).split())[:500]} Changes: {ch or 'none recorded'}. {cmd[0].upper() + cmd[1:]}."
            "\n\n"]


def render_report(run_dir: str, backend=AB, status: str | None = None) -> str:
    """report.md = the run status line, the writer's draft, and the blocks the harness renders from
    artifacts: the headline numbers, the declared limits and open findings (in full, with their
    required remedy), and the deliverable file the spec names."""
    import yaml
    draft = open(os.path.join(run_dir, "manuscript.md")).read()
    lp = os.path.join(run_dir, "models", "claims_ledger.yaml")
    if os.path.exists(lp):
        # The writer writes [CLAIM:id]; the harness substitutes the value (no number is typed).
        import render_claims as RCL
        try:
            # the valid claims; an invalid one stays a reference, reported by report_claim_findings
            draft, _unresolved = RCL.render_text(draft, RCL.load_claims_tolerant(lp)[0])
        except Exception:  # noqa: BLE001 -- left unrendered; report_claim_findings flags it
            pass
    h = backend.headline(run_dir)
    lines = ["", "<!-- harness: rendered from analysis/analysis.json -->", "## Numbers (rendered by the harness)", "",
             "| quantity | value |", "|---|---|"]
    lines += [f"| {k} | {v:,.4g} |" if isinstance(v, (int, float)) else f"| {k} | {v} |" for k, v in h.items()]
    try:    # engine parameter values the signed spec sets, each with the card value it replaces (HIGH when outside)
        from toolkit.spec import engine_parameters as PV
        sv = PV.report_block(run_dir)
    except Exception as e:  # noqa: BLE001 -- fail closed: the report says the block could not be rendered
        sv = ["## Engine parameter values set by the signed spec", "",
              f"Could not be rendered from analysis/engine.yaml: {type(e).__name__}: {e}", ""]
    if sv:
        lines += [""] + sv[:-1]
    decl, sd_find = [], []
    if os.path.exists(os.path.join(run_dir, "scope_declaration.yaml")):
        try:   # every shape the gate reads, each entry's full text (lib.scope_declarations)
            from lib import scope_declarations as SD
            _doc, decl, sd_find = SD.load(run_dir)
        except Exception as e:  # noqa: BLE001 -- fail closed: the report says so
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
                  f"{e.get('required_remedy') or 'not stated by the finding.'}" for e in dec2["declared"]]
    try:            # v2 autonomy 1B-ii: who drafted and signed the evidence dossier, and its declared gaps
        from toolkit.spec import evidence_draft as EDR
        so = {}
        for f in (SIGNOFF_FILE, "decision_brief.signoff.json"):     # Codex r1 P2-11: the record that binds the spec
            try:
                d_ = json.load(open(os.path.join(run_dir, f)))
            except (OSError, ValueError):
                continue
            binds = isinstance(d_, dict) and ((d_.get("sha256") or {}).get(AB.SPEC_FILE) if isinstance(
                d_.get("sha256"), dict) else None) or (isinstance(d_, dict) and d_.get("spec_sha256"))
            if binds and (d_.get("signed_by") or d_.get("by")):     # r2 P2-5: a brief that never bound the spec
                so = d_
                break
        eb = EDR.report_block(run_dir, so.get("signed_by") or so.get("by"))
    except Exception as e:  # noqa: BLE001 -- fail closed: the report says the block could not be rendered
        eb = ["## Evidence dossier", "", f"Could not be rendered from the evidence record: {type(e).__name__}.", ""]
    if eb:
        lines += [""] + eb[:-1]
    from toolkit.analysis import conflicts as CF       # W0-F: every held-out calibration target
    cl = CF.report_lines(run_dir)
    if cl:
        lines += [""] + cl[:-1]
    wmp = os.path.join(run_dir, "world_model", "world_model.md")    # §14: rendered by the harness from score.json
    try:
        if os.path.isfile(wmp) and not os.path.islink(wmp):
            lines += ["", open(wmp, encoding="utf-8").read().rstrip()]
    except (OSError, ValueError) as e:      # fail soft: the report is rendered without it, and says so
        lines += ["", f"## World model\n\nThe world-model section could not be read: {type(e).__name__}."]
    top, prov = provisional_lines(run_dir)
    lines += prov
    head = _status_line(run_dir, status) + "".join(amendment_lines(run_dir)) + "".join(top)
    body = draft.rstrip() + "\n"
    from toolkit.spec import review_conditions as RCN       # W0-G: conditions the lead deferred, at the top
    rcl = RCN.report_lines(run_dir)
    if rcl:
        head = head + "\n".join(rcl) + "\n"
    full = head + body + "\n".join(lines) + "\n"
    _put(run_dir, "report.md", full)
    # What the harness added, authenticated by the report's hash: writer_qa checks only the writer's span.
    _write_json(run_dir, "report_render.json", {"report_sha256": hashlib.sha256(full.encode("utf-8")).hexdigest(),
                                                "writer_span": [len(head), len(head) + len(body)]})
    _write_deliverable(run_dir)
    return os.path.join(run_dir, "report.md")


def _write_deliverable(run_dir: str) -> None:
    """The spec may name a deliverable file and its columns (from the allocation the harness
    computed): `deliverable_file: {path, columns: {out_col: allocation_col}, package_names: {...}}`."""
    import csv
    import yaml
    spec = yaml.safe_load(open(os.path.join(run_dir, AB.SPEC_FILE))) or {}
    df = spec.get("deliverable_file")
    src = os.path.join(run_dir, AB.ALLOCATION_CSV)
    if not isinstance(df, dict) or not os.path.exists(src) or deliverable_problems(run_dir):
        return                      # an invalid destination is a HIGH finding, never written
    rows = list(csv.DictReader(open(src)))
    names = df.get("package_names") or {}
    import io
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    cols = df["columns"]
    w.writerow(list(cols))
    for r in rows:
        w.writerow([names.get(r[c], r[c]) if c == "package" else r[c] for c in cols.values()])
    _put(run_dir, str(df["path"]), buf.getvalue())
