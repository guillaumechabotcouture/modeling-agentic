#!/usr/bin/env python3
"""amend: one path from a finished or blocked v2 run to a re-signable child run (Wave 0, W0-H1).

    python3 -m toolkit.spec.amend <parent_run> --patch <amendment.yaml> [--from-request] [--out DIR]
                                  [--signoff owner|lead]
    python3 -m toolkit.spec.amend --self-test

The parent is never written. The child is a new directory beside it (`<parent>.amend<k>`, or --out), holding:
  1. `metadata.json` with `parent_run` ({run_dir, run_state_sha256}, the form #311 records from MA_PARENT_RUN),
     `parent_spec_sha256` and `code_pin` (scripts/lib/pipeline_id.py code_pin: the commit, clean, on a remote);
  2. the parent's spec and evidence dossier with the patch applied, and its inputs and stored engine batches; never
     its run state, sign-off records, sheet, findings, critiques, report, figures or typed analysis outputs;
  3. a spec that validates (schema), the spec critique's counts, and review conditions imported when
     toolkit/spec/review_conditions.py (W0-G) is on this branch;
  4. a regenerated sign-off sheet, and the result of signing a scratch copy (proof the signature would verify);
  5. the reuse report: per stored batch, rows reusable under the current fingerprint rules and rows to compute, and
     why (scripts/compute.py reuse_report; the joint calibration's reuse_check when it has stored calls);
  6. `run_state.json` at SIGN, paused awaiting_signoff, with the parent's question, rounds, budget and sign-off mode;
  7. `amendment_record.json`: all of the above, the changes by path (before and after), and the commands: the one
     owner sign command (spec, sheet and derivation sha256 in full) and the launch command with smoke markers.
Under --signoff lead the child resumes under the lead's signature where the signer allows it (a departure still
needs the owner); the dry-sign is done as that signer.

Patch file:
    id: A1                                    # optional label
    reason: why, in words                     # required
    spec:                                     # operations on decision_spec.yaml, in order
      - {set: objective.horizon, value: [0, 5]}            # replace a value that exists
      - {add: analysis.decision_tests, value: {...}}       # append to a list, or add a key a mapping lacks
      - {remove: "analysis.decision_tests[id=DT3]"}        # delete a key or a list element that exists
    dossier: [...]                            # the same operations on the evidence dossier the spec names
Paths are dotted keys; a list element by index (`.0`) or by `[key=value]` (exactly one match). A path that does
not resolve, an operation on the wrong kind of node, a patch that changes nothing or a spec that no longer
validates is refused, and no child is made.

--from-request: the parent's spec_rethink_request.json (the stage machine's pause for a rethink of a signed spec)
must name the parent's signed spec. Its briefs and findings are recorded as the amendment's why; its `patch` (same
format) is applied when present, else --patch is required: the fix planner writes prose briefs, not a patch.
Nothing here names a disease, a place or an intervention.
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (ROOT, os.path.join(ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

RECORD = "amendment_record.json"
PRODUCER = "toolkit/spec/amend.py"
SPEC = "decision_spec.yaml"
STATE = "run_state.json"
REQUEST = "spec_rethink_request.json"         # agents/stage_machine.py SPEC_RETHINK_REQUEST
FINAL_OR_PAUSED = "FINAL"
# What a child never inherits: the parent's state and records of its own run, its sign-off and what was rendered
# after SIGN. Top-level names (files or directories).
NOT_COPIED = {STATE, "metadata.json", RECORD, REQUEST, "decision_spec.signoff.json", "decision_brief.signoff.json",
              "decision_brief.signed", "decision_spec.signoff_sheet.md", "scope_signoff.json",
              "scope_signoff_request.json", "report.md", "report_withheld.md", "manuscript.md", "final_gate.json",
              "findings.jsonl", "declared_findings.yaml", "engine_choice.json", "figures", "trace.jsonl",
              "budget_ledger.json", "progress.md", "copied_run.json", "_task_input.json"}
NOT_COPIED_PREFIX = ("critique_", "fix_plan_", "fix_impact")
OPS = ("set", "add", "remove")


class AmendError(ValueError):
    """The amendment is refused; no child run is made."""


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _derived_paths() -> set:
    """Run-relative files the harness writes after SIGN: typed analysis artifacts (toolkit/analysis/artifacts.py
    KINDS, carrying the parent's spec sha256), the manifest, the geometry sign-off record and the harness outputs
    under models/ that the reuse fingerprint already ignores."""
    from toolkit.analysis import artifacts as A
    from toolkit.spec import geometry as G
    import compute
    return ({f"analysis/{k}.yaml" for k in A.KINDS} | {A.MANIFEST.replace(os.sep, "/"), "analysis/blocked.yaml",
                                                       G.SIGNOFF_FILE} | set(compute.FP_HARNESS_OUTPUTS))


def _skip(rel: str, derived: set) -> bool:
    parts = rel.split("/")
    return (parts[0] in NOT_COPIED or (len(parts) == 1 and parts[0].startswith(NOT_COPIED_PREFIX))
            or parts[-1].endswith(".lock") or ".before_" in parts[-1] or rel in derived)


# ------------------------------------------------------------------ the patch
_SEL = re.compile(r"^([^\[\]]*)\[([^=\]]+)=([^\]]*)\]$")


def _steps(path) -> list:
    if not isinstance(path, str) or not path.strip():
        raise AmendError(f"path {path!r}: not a dotted path")
    out = []
    for seg in path.split("."):
        m = _SEL.match(seg)
        if m:
            out += ([m.group(1)] if m.group(1) else []) + [(m.group(2), m.group(3))]
        elif seg.isdigit():
            out.append(int(seg))
        elif seg and "[" not in seg and "]" not in seg:
            out.append(seg)
        else:
            raise AmendError(f"path {path!r}: segment {seg!r} is not a key, an index or [key=value]")
    return out


def _locate(doc, path):
    """(container, key, exists) of `path` in `doc`; every step but the last must exist."""
    steps, node = _steps(path), doc
    for j, s in enumerate(steps):
        if isinstance(s, tuple):
            hits = [i for i, x in enumerate(node) if isinstance(x, dict) and str(x.get(s[0])) == s[1]] \
                if isinstance(node, list) else None
            if hits is None or len(hits) != 1:
                raise AmendError(f"{path}: [{s[0]}={s[1]}] matches {'no list' if hits is None else len(hits)} "
                                 "element(s), not one")
            k, exists = hits[0], True
        elif isinstance(s, int) and isinstance(node, list):
            k, exists = s, 0 <= s < len(node)
            if not exists:
                raise AmendError(f"{path}: index {s} is outside the list ({len(node)} elements)")
        elif isinstance(node, dict):
            k, exists = s, s in node
        else:
            raise AmendError(f"{path}: {s!r} steps into a {type(node).__name__}")
        if j == len(steps) - 1:
            return node, k, exists
        if not exists:
            raise AmendError(f"{path}: {s!r} not found")
        node = node[k]


def apply_ops(doc, ops, what: str) -> tuple:
    """(new doc, changes) after the operations; AmendError on any that does not apply. `doc` is not modified."""
    if not isinstance(ops, list) or not ops:
        raise AmendError(f"{what}: the operations must be a non-empty list")
    doc, changes = copy.deepcopy(doc), []
    for i, op in enumerate(ops, 1):
        kinds = [k for k in OPS if isinstance(op, dict) and k in op]
        if not isinstance(op, dict) or len(kinds) != 1 or set(op) - set(OPS) - {"value"}:
            raise AmendError(f"{what} operation {i}: exactly one of set, add, remove (and value): {op!r}")
        kind, path = kinds[0], op[kinds[0]]
        if (kind == "remove") == ("value" in op):
            raise AmendError(f"{what} operation {i} ({path}): remove takes no value; set and add need one")
        node, k, exists = _locate(doc, path)
        before = copy.deepcopy(node[k]) if exists else None
        if kind == "set":
            if not exists:
                raise AmendError(f"{path}: not found (set replaces a value that exists; add creates one)")
            node[k] = op["value"]
        elif kind == "add":
            if exists and isinstance(node[k], list):
                node[k].append(op["value"])
            elif exists:
                raise AmendError(f"{path}: exists and is not a list (use set)")
            else:
                node[k] = op["value"]
        else:
            if not exists:
                raise AmendError(f"{path}: not found, nothing to remove")
            del node[k]
        changes.append({"file": what, "op": kind, "path": path, "before": before, "after": op.get("value")})
    return doc, changes


def _dump(doc, original: bytes, note: str) -> bytes:
    """The amended YAML: the original's leading comment block, one line naming the amendment, then the document.
    Comments inside the document are not carried (PyYAML); the record's changes say what moved."""
    import yaml
    head = []
    for line in original.decode("utf-8", "replace").splitlines(keepends=True):
        if line.strip() and not line.lstrip().startswith("#"):
            break
        head.append(line)
    return ("".join(head) + f"# {note}\n" + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120)
            ).encode("utf-8")


def load_patch(path, request) -> dict:
    import yaml
    src = (request or {}).get("patch") if request is not None else None
    if src is None:
        if not path:
            raise AmendError("no patch: pass --patch (" + ("the request carries none" if request is not None
                                                          else "or --from-request") + ")")
        try:
            src = yaml.safe_load(open(path, encoding="utf-8"))
        except (OSError, yaml.YAMLError, UnicodeDecodeError) as e:
            raise AmendError(f"{path}: {e}") from None
    elif path:
        raise AmendError("the request carries a patch: pass --patch or --from-request's patch, not both")
    if not isinstance(src, dict) or set(src) - {"id", "reason", "spec", "dossier"}:
        raise AmendError("a patch is a mapping of id, reason, spec and dossier")
    if not isinstance(src.get("reason"), str) or not src["reason"].strip():
        raise AmendError("a patch needs a reason, in words")
    if not src.get("spec") and not src.get("dossier"):
        raise AmendError("a patch needs spec or dossier operations")
    return src


# ------------------------------------------------------------------ the parent
def _parent(parent: str) -> tuple:
    """(run_state dict, its sha256, spec bytes, signed spec sha256 or None); refuses a running or unreadable run."""
    from lib.safe_io import LinkRefused, read_nofollow
    from toolkit.spec import signoff as SO
    try:
        sb, _ = read_nofollow(parent, STATE)
        st = json.loads(sb)
        spec = read_nofollow(parent, SPEC)[0]
    except (OSError, ValueError, LinkRefused) as e:
        raise AmendError(f"{parent}: not a readable v2 run ({e})") from None
    if not isinstance(st, dict) or not (st.get("state") == FINAL_OR_PAUSED or st.get("paused")):
        raise AmendError(f"{parent}: the run is neither finished nor paused (state {st.get('state')!r}); stop it first")
    signed = SO.signed_sha(parent)
    if signed is None:
        try:
            signed = json.loads(read_nofollow(parent, "decision_spec.signoff.json")[0]).get("spec_sha256")
        except (OSError, ValueError, LinkRefused, AttributeError):
            signed = None
    return st, _sha(sb), spec, signed


def _request(parent: str, signed) -> dict:
    from lib.safe_io import LinkRefused, read_nofollow
    try:
        rq = json.loads(read_nofollow(parent, REQUEST)[0])
    except (OSError, ValueError, LinkRefused) as e:
        raise AmendError(f"{REQUEST}: {e}") from None
    if not isinstance(rq, dict) or not signed or rq.get("signed_spec_sha256") != signed:
        raise AmendError(f"{REQUEST} was made for another signed spec (or the parent is not signed): refused")
    return rq


def _copy_tree(parent: str, stage: str) -> int:
    """Copy what the child inherits, through lib.safe_io; a link anywhere in the parent is refused. Modification
    times are kept (the reuse fingerprint hashes large files by size and mtime)."""
    from lib.safe_io import read_nofollow, write_nofollow
    derived, n = _derived_paths(), 0
    for dp, dirs, fns in os.walk(parent):
        for x in dirs + fns:
            if os.path.islink(os.path.join(dp, x)):
                raise AmendError(f"{os.path.relpath(os.path.join(dp, x), parent)}: a link in the parent run, refused")
        rel_dir = os.path.relpath(dp, parent)
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git")
                         and not _skip(os.path.normpath(os.path.join(rel_dir, d)).replace(os.sep, "/"), derived))
        for f in sorted(fns):
            rel = os.path.normpath(os.path.join(rel_dir, f)).replace(os.sep, "/")
            if _skip(rel, derived):
                continue
            data, st = read_nofollow(parent, rel)
            write_nofollow(stage, rel, data, exclusive=True)
            os.utime(os.path.join(stage, rel), ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
            n += 1
    return n


# ------------------------------------------------------------------ the checks
def _job_keyed(child: str, spec_path: str) -> dict:
    """{batch file: (rows, reusable or None, rule)} for the trusted row files whose rows are reused by job, whatever
    compute.py's fingerprint says: the joint calibration's calls and the batches of toolkit/analysis/row_reuse.py. A
    row qualifies with the current engine identity and adapter digest (its job_sha is matched when the run builds its
    jobs). A file with no job-keyed rows is left to compute.py's rule."""
    from toolkit.analysis import joint_calibration as JC, row_reuse as RR
    try:
        from toolkit.engines.contract import engine_identity
        from toolkit.spec import schema
        ident = engine_identity(schema.load(spec_path).engine())
    except Exception:  # noqa: BLE001 -- engine: auto is chosen after SIGN: rows are listed, not counted
        ident = None
    adapter = JC.adapter_digest()
    legacy = os.environ.get(JC.LEGACY_ADAPTER_ENV) == adapter
    got = {}
    for dp, dirs, _ in os.walk(os.path.join(child, "analysis")):
        dirs[:] = [d for d in dirs if d != RR.ARCHIVE]        # an archive is read with its directory (RR._sources)
        jc = os.path.dirname(dp) == os.path.join(child, JC.OUT_REL)
        try:
            ad = json.load(open(os.path.join(dp, "adapter.json"))).get("adapter_sha256") if jc else None
        except (OSError, ValueError, AttributeError):
            ad = None
        for f, kind, fad in ([(f, k, ad) for f, k, _ in JC._trusted_sources(dp)] if jc else RR._sources(dp)):
            rows = JC._source_rows(f, kind)
            if rows:
                n = None if ident is None else sum(
                    b.get("provenance", {}).get("engine") == ident and (fad == adapter or (fad is None and legacy))
                    for _, b in rows)
                got[os.path.relpath(f, child)] = (len(rows), n, "joint calibration" if jc else "row_reuse")
    return got


def reuse(child: str, spec_path: str) -> dict:
    """Per stored batch: rows reusable under the current rules, rows to compute, which rule decides, and why."""
    out = {"rule": "job-keyed batches (joint calibration, row_reuse): a row whose engine identity and adapter digest "
                   "are the current ones; other batches: scripts/compute.py's code fingerprint (function file, repo "
                   "scripts/ and toolkit/, the run's models/ and data/). Either way a row answers only the same job, "
                   "known when the run builds its jobs: reusable rows are eligible, not promised"}
    try:
        import compute
        r = compute.reuse_report(child)
        try:
            jk = _job_keyed(child, spec_path)
        except Exception as e:  # noqa: BLE001 -- fail closed: compute's rule only, the reason recorded
            jk, out["job_keyed_error"] = {}, f"{type(e).__name__}: {e}"
        out["batches"] = []
        for b in r["batches"]:
            row = {"out": b["out"], "fn": b["fn"], "stored": b["stored"], "reusable": b["eligible"],
                   "rule": "compute fingerprint", "why": b["why"] or "same function and code fingerprint"}
            if b["out"] in jk:
                n, ok_, rule = jk[b["out"]]
                row.update(stored=n, reusable=ok_ or 0, rule=f"job-keyed ({rule})",
                           why="engine identity and adapter digest current" if ok_ == n else (
                               "engine chosen after SIGN: not counted" if ok_ is None
                               else "engine identity or adapter digest changed for some rows"))
            row["to_compute"] = row["stored"] - row["reusable"]
            out["batches"].append(row)
        out["stored"] = sum(b["stored"] for b in out["batches"])
        out["reusable"] = sum(b["reusable"] for b in out["batches"])
    except Exception as e:  # noqa: BLE001 -- fail closed: no counts, the reason recorded
        out["error"] = f"compute.reuse_report: {type(e).__name__}: {e}"
    return out


def _review_conditions(child: str, parent: str) -> dict:
    """W0-G (#308): import the spec critique's and the parent's open conditions when its module is on this branch."""
    mod = os.path.join(ROOT, "toolkit", "spec", "review_conditions.py")
    if not os.path.isfile(mod):
        return {"status": "not on this branch (W0-G)"}
    r = subprocess.run([sys.executable, mod, "import", child, "--critique", "--parent", parent],
                       capture_output=True, text=True, timeout=600)
    return {"status": "imported" if r.returncode == 0 else "failed", "rc": r.returncode,
            "output": (r.stdout + r.stderr)[-1500:]}


def dry_sign(child: str, by: str, expect: dict) -> dict:
    """Sign a scratch copy with the hashes the owner will be shown; the child itself stays unsigned."""
    from toolkit.spec import signoff as SO
    with tempfile.TemporaryDirectory() as td:
        cp = os.path.join(td, "run")
        shutil.copytree(child, cp, symlinks=True)
        try:
            SO.sign(cp, by, "amend dry-sign (scratch copy)", **expect)
            st = SO.status(cp)
            return {"ok": st == "signed", "by": by, "status": st}
        except ValueError as e:
            return {"ok": False, "by": by, "error": str(e)[:3000]}


def _critique(child: str, spec_doc) -> dict:
    try:
        from toolkit.spec import spec_critique as SC
        fs = SC.findings(spec_doc, child)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    n = {s: sum(f["severity"] == s for f in fs) for s in ("HIGH", "MEDIUM", "DECLARED")}
    return {**{k.lower(): v for k, v in n.items()},
            "open": [{"check": f["check"], "severity": f["severity"]} for f in fs if f["severity"] in ("HIGH", "MEDIUM")]}


# ------------------------------------------------------------------ amend
def _next_child(parent: str) -> str:
    k = 1
    while os.path.lexists(f"{parent}.amend{k}") or os.path.lexists(f"{parent}.amend{k}.partial"):
        k += 1
    return f"{parent}.amend{k}"


def _file_sha(d: str, rel: str) -> str:
    p = os.path.join(d, rel)
    return _sha(open(p, "rb").read()) if os.path.isfile(p) else "none"


def amend(parent, patch=None, from_request=False, out=None, signoff=None, repo=ROOT, log=print) -> dict:
    """Make the child run (module docstring) and return its amendment record. AmendError: refused, no child."""
    import yaml
    from lib import pipeline_id
    from lib.safe_io import write_nofollow
    from toolkit.spec import evidence_dossier as ED, schema, signoff as SO
    parent = os.path.realpath(parent)
    st, st_sha, spec_bytes, signed = _parent(parent)
    request = _request(parent, signed) if from_request else None
    p = load_patch(patch, request)
    spec0 = yaml.safe_load(spec_bytes.decode("utf-8")) or {}
    spec1, changes = apply_ops(spec0, p["spec"], SPEC) if p.get("spec") else (spec0, [])
    drel = ED.dossier_reference(spec1)
    dossier = None
    if p.get("dossier"):
        if not drel or not os.path.isfile(os.path.join(parent, drel)):
            raise AmendError("dossier operations, but the spec names no evidence dossier in the parent")
        dbytes = ED.read_dossier(parent, drel)
        d0 = yaml.safe_load(dbytes.decode("utf-8")) or {}
        d1, dch = apply_ops(d0, p["dossier"], drel)
        if d1 != d0:
            dossier = _dump(d1, dbytes, f"Amended by {PRODUCER} ({p.get('id') or 'unnamed'})")
        changes += dch
    if spec1 == spec0 and dossier is None:
        raise AmendError("the patch changes nothing")
    label = str(p.get("id") or "unnamed")
    reason1 = " ".join(p["reason"].split())
    new_spec = _dump(spec1, spec_bytes, f"Amendment {label} by {PRODUCER} from parent spec sha256 "
                                        f"{signed or _sha(spec_bytes)}: {reason1}")
    child = os.path.realpath(out) if out else _next_child(parent)
    if os.path.lexists(child):
        raise AmendError(f"{child} exists")
    stage = child + ".partial"
    os.mkdir(stage)
    try:
        n = _copy_tree(parent, stage)
        write_nofollow(stage, SPEC, new_spec, atomic=True)
        if dossier is not None:
            write_nofollow(stage, drel, dossier, atomic=True)
        sp = schema.load(os.path.join(stage, SPEC), data=new_spec)
        errs = schema.validate(sp, pending_ok=True)
        if errs:
            raise AmendError("the amended spec does not validate: " + "; ".join(map(str, errs[:10])))
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    os.rename(stage, child)
    mode = signoff or st.get("signoff") or "owner"
    pin = pipeline_id.code_pin(repo)
    meta = {"pipeline": "v2", "question": st.get("question"), "signoff": mode, "max_rounds": st.get("max_rounds"),
            "parent_run": {"run_dir": parent, "run_state_sha256": st_sha}, "parent_spec_sha256": signed,
            "parent_spec_signed": bool(signed), "code_pin": pin, "amendment": label,
            "producer": PRODUCER, "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
    write_nofollow(child, "metadata.json", json.dumps(meta, indent=2).encode(), atomic=True)
    keep = ("question", "max_rounds", "gate_mode", "budget_usd", "task_cap_usd", "v2_rules", "derived_inputs",
            "derive_unsandboxed", "world_model_stages")
    cst = {**{k: st[k] for k in keep if k in st}, "state": "SIGN", "round": 1, "signoff": mode, "spent_usd": 0.0,
           "paused": "awaiting_signoff", "status": None, "tries": {}, "journal": {},
           "history": [{"at": meta["created"], "from": None, "to": "SIGN",
                        "why": f"amendment {label} of {parent} ({len(changes)} change(s)): {reason1}"[:500]}]}
    write_nofollow(child, STATE, json.dumps(cst, indent=1).encode(), atomic=True)
    rc = _review_conditions(child, parent)
    SO.write_sheet(child)
    expect = {"expect_sha256": _file_sha(child, SPEC), "expect_sheet_sha256": _file_sha(child, SO.SHEET),
              "expect_derivations_sha256": _file_sha(child, SO.DERIVATIONS_RECORD)}
    q = shlex.quote(child)
    commit = pin.get("commit", "unknown").removesuffix("-dirty")
    budget = f" --max-budget-usd {st['budget_usd']:g}" if isinstance(st.get("budget_usd"), (int, float)) else ""
    commands = {
        "owner_sign": f"python3 agents/stage_machine.py sign {q} --by owner --expect-sha256 {expect['expect_sha256']} "
                      f"--expect-sheet-sha256 {expect['expect_sheet_sha256']} "
                      f"--expect-derivations-sha256 {expect['expect_derivations_sha256']}",
        "launch": f"python3 main.py --v2 --resume {q} --signoff {mode} "
                  f"--max-rounds {st.get('max_rounds') or 3}{budget}",
        "smoke": f"python3 scripts/smoke_check.py {q} --pipeline v2 --commit {commit} "
                 "--expect 'metadata.json:parent_run' --expect 'metadata.json:code_pin' "
                 "--expect 'amendment_record.json:reuse' --expect 'run_state.json~spec signed by' "
                 "--expect 'run_state.json~\"to\": \"EVIDENCE\"'"}
    rec = {"schema": "amendment_record/1", "producer": PRODUCER, "child": child, "id": label, "reason": p["reason"],
           "parent_run": meta["parent_run"], "parent_spec_sha256": signed, "files_copied": n,
           "request": ({k: request.get(k) for k in ("round", "requested_at", "briefs", "findings")}
                       if request is not None else None),
           "changes": changes, "spec_sha256": expect["expect_sha256"], "sheet_sha256": expect["expect_sheet_sha256"],
           "derivations_sha256": expect["expect_derivations_sha256"], "spec_critique": _critique(child, spec1),
           "review_conditions": rc, "dry_sign": dry_sign(child, mode, expect),
           "reuse": reuse(child, os.path.join(child, SPEC)), "code_pin": pin, "signoff": mode, "commands": commands}
    write_nofollow(child, RECORD, json.dumps(rec, indent=1, default=str).encode(), atomic=True)
    _print(rec, log)
    return rec


def _print(rec: dict, log) -> None:
    short = lambda v: (json.dumps(v, default=str)[:160] if v is not None else "(absent)")  # noqa: E731
    log(f"Child run: {rec['child']} (amendment {rec['id']}; parent spec {rec['parent_spec_sha256']})")
    log("Changes:")
    for c in rec["changes"]:
        log(f"  {c['op']} {c['file']}:{c['path']}: {short(c['before'])} -> {short(c['after'])}")
    sc = rec["spec_critique"]
    log(f"Spec critique: {sc.get('high')} HIGH, {sc.get('medium')} MEDIUM, {sc.get('declared')} declared"
        + (f" ({sc['error']})" if sc.get("error") else ""))
    log(f"Review conditions: {rec['review_conditions']['status']}")
    r = rec["reuse"]
    log(f"Reuse: {r.get('reusable')} of {r.get('stored')} stored rows reusable" + (f" ({r['error']})" if "error" in r
                                                                                    else ""))
    for b in r.get("batches", []):
        log(f"  {b['out']}: {b['reusable']} reusable, {b['to_compute']} to compute; {b['rule']}: {b['why']}")
    pin = rec["code_pin"]
    log(f"Code pin: {pin.get('commit')} (clean: {pin.get('clean')}, on a remote: {pin.get('on_remote')})")
    ds = rec["dry_sign"]
    if not ds["ok"]:
        log(f"Dry-sign as {ds['by']} FAILED on a scratch copy: {ds.get('error') or ds.get('status')}")
        log("Fix the patch and amend again; no sign command is given.")
        return
    log(f"Dry-sign as {ds['by']}: a scratch copy signed and verified.")
    if rec["signoff"] == "owner":
        log("Owner sign command:\n  " + rec["commands"]["owner_sign"])
    else:
        log("Lead mode: the lead signs at SIGN when the run resumes (an owner may sign first with:\n  "
            + rec["commands"]["owner_sign"] + " )")
    log(f"Launch, after signing, from a checkout at {pin.get('commit')}:\n  " + rec["commands"]["launch"])
    log("Smoke markers:\n  " + rec["commands"]["smoke"])


def _self_test() -> int:
    import hashlib
    import json
    import shutil
    import tempfile
    import yaml
    from toolkit.spec import schema, signoff as SO
    import compute
    from lib import pipeline_id
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def tree(d):
        h = hashlib.sha256()
        for dp, dirs, fns in os.walk(d):
            dirs.sort()
            for f in sorted(fns):
                p = os.path.join(dp, f)
                h.update(os.path.relpath(p, d).encode() + b"\0" + open(p, "rb").read())
        return h.hexdigest()

    def sha(p):
        return hashlib.sha256(open(p, "rb").read()).hexdigest()

    def refused(fn, m):
        try:
            fn()
            ok(False, m)
        except AmendError as e:
            ok(True, f"{m} ({str(e)[:90]})")
        except Exception as e:  # noqa: BLE001
            ok(False, f"{m}: {type(e).__name__}: {e}")

    def w(root, rel, txt):
        os.makedirs(os.path.dirname(os.path.join(root, rel)) or root, exist_ok=True)
        open(os.path.join(root, rel), "w").write(txt)

    with tempfile.TemporaryDirectory() as td:
        td = os.path.realpath(td)
        par = os.path.join(td, "parent")
        os.makedirs(par)
        w(par, "metadata.json", json.dumps({"pipeline": "v2", "commit": "0" * 40}))
        schema.toy_spec(par)
        SO.write_sheet(par)
        SO.sign(par, "owner")
        psig = SO.signed_sha(par)
        w(par, "run_state.json", json.dumps({"question": "q", "state": "FINAL", "status": "completed_gate_not_accepted",
                                             "signoff": "owner", "max_rounds": 2, "budget_usd": 5.0, "spent_usd": 3.0,
                                             "derived_inputs": True, "history": [{"to": "FINAL"}]}))
        for rel in ("report.md", "critique_domain.yaml", "findings.jsonl", "analysis/manifest.json",
                    "analysis/point.yaml", "figures/a.png", "engine_choice.json"):
            w(par, rel, "stale\n")
        w(par, "data/derived.csv", "u,v\n1,2\n")
        w(par, "analysis/benchmarks_at_spec_values.record.json", "{}\n")
        w(par, "models/fn.py", "def f(p):\n    return {'y': p['x']}\n")
        w(par, "gone.py", "def f(p):\n    return {'y': 2 * p['x']}\n")
        for fn, out in (("models/fn.py::f", "analysis/batch/points.jsonl"), ("gone.py::f", "analysis/b2/points.jsonl")):
            compute.run_map(par, fn, [{"x": i} for i in range(3)], os.path.join(par, out), backend="local",
                            max_workers=1, ledger=False)
        os.remove(os.path.join(par, "gone.py"))
        p_rows = compute.reuse_report(par)
        before = tree(par)
        pf = os.path.join(td, "a1.yaml")
        yaml.safe_dump({"id": "A1", "reason": "raise the envelope", "spec": [
            {"set": "constraints.envelope_usd", "value": 9500},
            {"set": "analysis.decision_tests[id=DT1].text", "value": "Does the plan change?"}]}, open(pf, "w"))
        rec = amend(par, pf, log=lambda *a: None)
        child = rec.get("child", "")
        meta = json.load(open(os.path.join(child, "metadata.json")))
        cst = json.load(open(os.path.join(child, "run_state.json")))
        cspec = yaml.safe_load(open(os.path.join(child, "decision_spec.yaml")))
        ok(tree(par) == before and SO.status(par) == "signed" and SO.status(child) == "awaiting"
           and child == par + ".amend1",
           f"AM_CHILD_KEEPS_PARENT_SIGNED the parent is unchanged and signed; the child awaits a signature ({child})")
        ok(meta.get("parent_run", {}).get("run_dir") == par and meta.get("parent_spec_sha256") == psig
           and meta["parent_run"].get("run_state_sha256") == sha(os.path.join(par, "run_state.json"))
           and meta.get("pipeline") == "v2", f"AM_LINEAGE parent_run and parent_spec_sha256 recorded ({meta})")
        ok(cspec["constraints"]["envelope_usd"] == 9500 and cspec["analysis"]["decision_tests"][0]["text"]
           == "Does the plan change?" and [c["path"] for c in rec["changes"]] == [
               "constraints.envelope_usd", "analysis.decision_tests[id=DT1].text"]
           and rec["changes"][0]["before"] == 9000, f"AM_PATCH_APPLIED set by path and by [id=...] ({rec['changes']})")
        gone = [x for x in ("report.md", "critique_domain.yaml", "findings.jsonl", "analysis/manifest.json",
                            "analysis/point.yaml", "figures", "engine_choice.json", "decision_spec.signoff.json",
                            "decision_brief.signed", "data/geometry_signoff.json") if os.path.lexists(os.path.join(child, x))]
        kept = [x for x in ("data/derived.csv", "analysis/benchmarks_at_spec_values.record.json", "models/fn.py",
                            "analysis/batch/points.jsonl.summary.json", "units.csv")
                if not os.path.exists(os.path.join(child, x))]
        ok(not gone and not kept, f"AM_CHILD_CONTENT run outputs and sign-off records left out ({gone}); inputs and "
                                  f"stored rows kept ({kept})")
        ok(cst.get("state") == "SIGN" and cst.get("paused") == "awaiting_signoff" and cst.get("signoff") == "owner"
           and cst.get("spent_usd") == 0 and cst.get("budget_usd") == 5.0 and cst.get("max_rounds") == 2,
           f"AM_STATE the child waits at SIGN for the owner ({cst})")
        src = SO.sheet_sources(open(os.path.join(child, SO.SHEET)).read()) or {}
        ssha, shsha = sha(os.path.join(child, "decision_spec.yaml")), sha(os.path.join(child, SO.SHEET))
        cmd = rec.get("commands", {}).get("owner_sign", "")
        ok(src.get("spec_sha256") == ssha and rec.get("dry_sign", {}).get("ok") is True
           and f"--expect-sha256 {ssha}" in cmd and f"--expect-sheet-sha256 {shsha}" in cmd
           and SO.status(child) == "awaiting",
           f"AM_SHEET_MATCHES_SPEC the sheet shows the child spec; a copy dry-signs; the child stays unsigned ({cmd})")
        r = rec.get("reuse", {})
        bys = {b["out"]: b for b in r.get("batches", [])}
        ok(r.get("stored") == 6 and r.get("reusable") == 3 and bys.get("analysis/batch/points.jsonl", {}).get(
            "to_compute") == 0 and bys.get("analysis/b2/points.jsonl", {}).get("to_compute") == 3
           and "not found" in bys.get("analysis/b2/points.jsonl", {}).get("why", "")
           and p_rows["eligible"] == 3, f"AM_REUSE_REPORT_COUNTS per batch, rows reused and to compute, with why ({r})")
        pin = meta.get("code_pin") or {}
        ok(pin.get("commit") == pipeline_id.code_commit(ROOT) and "clean" in pin and "on_remote" in pin
           and rec.get("code_pin") == pin, f"AM_CODE_PIN_RECORDED ({pin})")
        launch = rec.get("commands", {}).get("launch", "")
        smoke = rec.get("commands", {}).get("smoke", "")
        ok("main.py --v2 --resume" in launch and "--signoff owner" in launch and "metadata.json:parent_run" in smoke
           and "metadata.json:code_pin" in smoke and "amendment_record.json:reuse" in smoke
           and os.path.isfile(os.path.join(child, RECORD)), f"AM_COMMANDS launch and smoke markers ({launch})")
        try:
            SO.sign(child, "owner", expect_sha256=ssha, expect_sheet_sha256=shsha,
                    expect_derivations_sha256=rec.get("derivations_sha256"))
            ok(SO.status(child) == "signed", "AM_OWNER_COMMAND_SIGNS the hashes printed sign the child")
        except ValueError as e:
            ok(False, f"AM_OWNER_COMMAND_SIGNS the hashes printed sign the child: {e}")
        # --from-request: the S7 pause's request, with a patch, names the parent's signed spec
        rq = {"round": 1, "signed_spec_sha256": psig, "briefs": [{"task": "T1", "findings": ["D-1"], "brief": "b"}],
              "findings": [{"id": "D-1", "severity": "HIGH", "claim": "c"}],
              "patch": {"reason": "from the request", "spec": [{"set": "constraints.envelope_usd", "value": 9100}]}}
        w(par, "spec_rethink_request.json", json.dumps(rq))
        rec2 = amend(par, from_request=True, signoff="lead", log=lambda *a: None)
        c2 = rec2.get("child", "")
        ok(c2 == par + ".amend2" and yaml.safe_load(open(os.path.join(c2, "decision_spec.yaml")))["constraints"][
            "envelope_usd"] == 9100 and rec2.get("request", {}).get("briefs") == rq["briefs"]
           and json.load(open(os.path.join(c2, "run_state.json")))["signoff"] == "lead"
           and rec2.get("dry_sign", {}).get("by") == "lead" and rec2["dry_sign"].get("ok") is True,
           f"AM_FROM_RETHINK_REQUEST the request's patch and why; a lead child dry-signs as the lead ({c2})")
        w(par, "spec_rethink_request.json", json.dumps(dict(rq, signed_spec_sha256="f" * 64)))
        refused(lambda: amend(par, from_request=True, log=lambda *a: None),
                "AM_STALE_REQUEST_REFUSED a request made for another signed spec")
        w(par, "spec_rethink_request.json", json.dumps({k: v for k, v in rq.items() if k != "patch"}))
        refused(lambda: amend(par, from_request=True, log=lambda *a: None),
                "AM_REQUEST_WITHOUT_PATCH_REFUSED a request with no patch and no --patch")
        os.remove(os.path.join(par, "spec_rethink_request.json"))
        n_dirs = sorted(os.listdir(td))
        bad = {"unresolved": {"reason": "r", "spec": [{"set": "constraints.nope", "value": 1}]},
               "two ops": {"reason": "r", "spec": [{"set": "constraints.envelope_usd", "remove": "x", "value": 1}]},
               "no reason": {"spec": [{"set": "constraints.envelope_usd", "value": 1}]},
               "no-op": {"reason": "r", "spec": [{"set": "constraints.envelope_usd", "value": 9000}]},
               "add existing": {"reason": "r", "spec": [{"add": "constraints.envelope_usd", "value": 1}]},
               "ambiguous selector": {"reason": "r", "spec": [{"remove": "analysis.decision_tests[id=DT9]"}]},
               "invalid spec": {"reason": "r", "spec": [{"set": "comparator.package", "value": "nope"}]},
               "not a mapping": ["x"]}
        for name, p in bad.items():
            yaml.safe_dump(p, open(pf, "w"))
            refused(lambda: amend(par, pf, log=lambda *a: None), f"AM_BAD_PATCH_REFUSED {name}")
        ok(sorted(os.listdir(td)) == n_dirs and tree(par) == before,
           "AM_BAD_PATCH_REFUSED leaves no child and the parent unchanged")
        yaml.safe_dump({"reason": "r", "spec": [{"set": "constraints.envelope_usd", "value": 9200}]}, open(pf, "w"))
        st = json.load(open(os.path.join(par, "run_state.json")))
        w(par, "run_state.json", json.dumps(dict(st, state="ANALYZE", status=None, paused=None)))
        refused(lambda: amend(par, pf, log=lambda *a: None), "AM_RUNNING_PARENT_REFUSED a parent neither final nor paused")
        w(par, "run_state.json", json.dumps(st))
        os.symlink(os.path.join(td, "a1.yaml"), os.path.join(par, "data", "link.yaml"))
        refused(lambda: amend(par, pf, log=lambda *a: None), "AM_LINK_REFUSED a parent holding a link")
        os.remove(os.path.join(par, "data", "link.yaml"))
        shutil.rmtree(c2)
    if fails:
        print(f"FAIL: {len(fails)} self-test case(s) failed")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    ap.add_argument("parent_run")
    ap.add_argument("--patch", default=None, help="the amendment (YAML: id, reason, spec, dossier)")
    ap.add_argument("--from-request", action="store_true", help=f"take the why (and a patch) from {REQUEST}")
    ap.add_argument("--out", default=None, help="the child run directory (default <parent>.amend<k>)")
    ap.add_argument("--signoff", choices=["owner", "lead"], default=None, help="default: the parent's mode")
    a = ap.parse_args(argv)
    try:
        rec = amend(a.parent_run, a.patch, a.from_request, a.out, a.signoff)
    except AmendError as e:
        print(f"Refused: {e}", file=sys.stderr)
        return 1
    return 0 if rec["dry_sign"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
