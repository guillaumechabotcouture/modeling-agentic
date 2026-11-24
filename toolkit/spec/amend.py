#!/usr/bin/env python3
"""amend: one path from a finished or blocked v2 run to a re-signable child run (Wave 0, W0-H1).

    python3 -m toolkit.spec.amend <parent_run> --patch <amendment.yaml> [--from-request] [--out DIR]
                                  [--signoff owner|lead]
    python3 -m toolkit.spec.amend --self-test

The parent is never written. The child (`<parent>.amend<k>`, or --out, outside the parent) holds: metadata.json
with `parent_run` ({run_dir, run_state_sha256}, #311's form), `parent_spec_sha256` and `code_pin`; the patched
spec and dossier, the parent's inputs and stored batches (never its state, sign-off records, sheet, findings,
dispositions, critiques, report, figures or analysis outputs); review conditions (W0-G) imported; a regenerated
sheet; amendment_record.json (changes by path, spec critique, dry-sign of a scratch copy, reuse report per batch,
code pin, the owner sign command with the sha256 values in full, the launch and smoke commands); and, written last,
run_state.json at SIGN, awaiting_signoff. Refused, with no child: a patch that does not apply or changes nothing,
a parent running or edited since signing, a link or unreadable directory in it, or a spec failing the checks the
SPEC stage runs before SIGN. A failed dry-sign keeps the child and prints no sign command.

Patch file: `id` (optional), `reason` (required), and `spec` / `dossier` lists of operations, in order:
    - {set: objective.horizon, value: [0, 5]}            # replace a value that exists
    - {add: analysis.decision_tests, value: {...}}       # append to a list, or add a key a mapping lacks
    - {remove: "analysis.decision_tests[id=DT3]"}        # delete a key or a list element that exists
Paths are dotted keys; a list element by index (`.0`) or `[key=value]` (exactly one element with that key).
--from-request: the parent's spec_rethink_request.json must name its signed spec; its briefs and findings are the
why, and its `patch` is applied when present, else --patch is required. Nothing here names a disease or a place.
Provenance: metadata.json and the record carry `motivated_by` (`motivation`); a patch may add `motivated_by:
{findings: [ids], reason}`. scripts/harness_metrics.py --check-value reads it.
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
              "budget_ledger.json", "progress.md", "copied_run.json", "_task_input.json", "done_when_registry.json",
              "scope_declaration.yaml", "impact_ledger.jsonl"}
NOT_COPIED_PREFIX = ("critique_", "fix_plan_", "fix_impact", "fix_progress")
NOT_COPIED_DIRS = ("models/decision_tests",)     # the parent's decision-test results: the child tests again
OPS = ("set", "add", "remove")


class AmendError(ValueError):
    """The amendment is refused; no child run is made."""


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def patch_sha256(patch) -> str:
    """The patch's content digest, recorded in the child's metadata: a lead recovering an interrupted hand-off adopts
    only the child its request's patch made (Codex r2 of #349)."""
    # in the patch's own key order (a YAML patch may mix key types, which do not sort: Codex r3); the request's patch
    # is read back from the same JSON in the same order
    return _sha(json.dumps(patch, default=str).encode())


def _derived_paths() -> set:
    """Files the harness writes after SIGN: typed analysis artifacts, the manifest, geometry sign-off, models/ outputs."""
    from toolkit.analysis import artifacts as A
    from toolkit.spec import geometry as G
    import compute
    return ({f"analysis/{k}.yaml" for k in A.KINDS} | {A.MANIFEST.replace(os.sep, "/"), "analysis/blocked.yaml",
                                                       G.SIGNOFF_FILE} | set(compute.FP_HARNESS_OUTPUTS))


def _skip(rel: str, derived: set) -> bool:
    parts = rel.split("/")
    return (parts[0] in NOT_COPIED or (len(parts) == 1 and parts[0].startswith(NOT_COPIED_PREFIX))
            or parts[-1].endswith(".lock") or ".before_" in parts[-1] or rel in derived
            or any(rel == d or rel.startswith(d + "/") for d in NOT_COPIED_DIRS))


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
            hits = [i for i, x in enumerate(node) if isinstance(x, dict) and s[0] in x and str(x[s[0]]) == s[1]] \
                if isinstance(node, list) else None
            if hits is None or len(hits) != 1:
                raise AmendError(f"{path}: [{s[0]}={s[1]}] matches {'no list' if hits is None else len(hits)} "
                                 "element(s), not one")
            k, exists = hits[0], True
        elif isinstance(node, list) and s.isdigit():
            k, exists = int(s), 0 <= int(s) < len(node)
            if not exists:
                raise AmendError(f"{path}: index {s} is outside the list ({len(node)} elements)")
        elif isinstance(node, dict):
            num = int(s) if s.isdigit() and str(int(s)) == s else None      # "1" may be the YAML integer key 1
            if num is not None and num in node and s in node:
                raise AmendError(f"{path}: {s} names both a number key and a text key")
            k = num if num is not None and num in node else s
            exists = k in node
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
            node[k] = copy.deepcopy(op["value"])
        elif kind == "add":
            if exists and isinstance(node[k], list):
                node[k].append(copy.deepcopy(op["value"]))
            elif exists:
                raise AmendError(f"{path}: exists and is not a list (use set)")
            else:
                node[k] = copy.deepcopy(op["value"])
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


def _no_aliases(data: bytes, what: str) -> None:
    """A YAML alias shares one node between paths, so an edit by one path would move another: refused."""
    import yaml
    try:
        if any(isinstance(e, yaml.AliasEvent) for e in yaml.parse(data.decode("utf-8"))):
            raise AmendError(f"{what} uses YAML aliases; write the shared values out before amending")
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        raise AmendError(f"{what} does not parse: {e}") from None


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
    if not isinstance(src, dict) or set(src) - {"id", "reason", "spec", "dossier", "motivated_by"}:
        raise AmendError("a patch is a mapping of id, reason, spec, dossier and motivated_by")
    if src.get("id") is not None and not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", str(src["id"])):
        raise AmendError("a patch id is 1 to 40 letters, digits, '_', '.' or '-'")
    if not isinstance(src.get("reason"), str) or not src["reason"].strip():
        raise AmendError("a patch needs a reason, in words")
    if not src.get("spec") and not src.get("dossier"):
        raise AmendError("a patch needs spec or dossier operations")
    return src


# ------------------------------------------------------------------ why: the parent's findings
MOTIVE = "motivated_by/1"
_MOTIVE_FIELDS = ("kind", "source", "severity", "status", "owner_state")
_list = lambda x: x if isinstance(x, list) else []       # noqa: E731


def _ledger_rows(parent: str):
    """{id: row} of the parent's findings.jsonl, or None when it has none; AmendError when it cannot be read."""
    from lib.safe_io import LinkRefused, read_nofollow
    try:
        data = read_nofollow(parent, "findings.jsonl")[0].decode("utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError, LinkRefused, UnicodeDecodeError) as e:
        raise AmendError(f"the parent's findings.jsonl cannot be read ({type(e).__name__})") from None
    try:
        rows = [json.loads(x) for x in data.splitlines() if x.strip()]
    except ValueError:
        rows = [None]
    if not all(isinstance(r, dict) and isinstance(r.get("id"), str) for r in rows):
        raise AmendError("the parent's findings.jsonl holds a line that is not a finding row")
    return {r["id"]: r for r in rows}


def _categories(parent: str) -> dict:
    """{(reviewer, id): category} from the parent's critique YAMLs (descriptive only: an unreadable file gives none)."""
    import yaml
    from lib.safe_io import read_nofollow
    import findings_ledger as FL
    out = {}
    for reviewer, name in FL.CRITIQUE_FILES.items():
        try:
            doc = yaml.safe_load(read_nofollow(parent, name)[0]) or {}
        except Exception:  # noqa: BLE001 -- absent or unreadable: no category, never a refusal
            continue
        for b in _list(doc.get("blockers") if isinstance(doc, dict) else None):
            if isinstance(b, dict) and b.get("id") is not None and b.get("category"):
                out[(reviewer, str(b["id"]))] = str(b["category"]).upper()
    return out


def motivation(parent: str, request=None, declared=None, required=False) -> dict:
    """The parent's findings behind an amendment or relaunch (id, kind, source, category, answer fingerprint from its
    ledger) and its run id. `required` (a rethink): the request names one, else AmendError. A patch's declared id must
    be in the ledger, and naming none needs a reason; a request id the ledger lacks is kept, `in_ledger: false`."""
    if os.path.join(ROOT, "agents") not in sys.path:
        sys.path.insert(0, os.path.join(ROOT, "agents"))
    import findings_ledger as FL
    ids, mine, reason = [], [], None
    if request is not None:
        ids = [f.get("id") if isinstance(f, dict) else f for f in _list(request.get("findings"))]
        ids += [x for b in _list(request.get("briefs")) if isinstance(b, dict) for x in _list(b.get("findings"))]
        bad = [x for x in ids if not (isinstance(x, str) and x.strip())]
        if bad:                       # a finding id is a non-empty string, never coerced (Codex r2 of #373)
            raise AmendError(f"the rethink request names a finding without a finding id: {bad[:3]!r}")
        ids = [x.strip() for x in ids]
    if required and not ids:          # the request's own findings: a patch's declaration does not stand in for them
        raise AmendError("a rethink-driven amendment records the findings that motivated it: the request names none")
    if declared is not None:
        if not isinstance(declared, dict) or set(declared) - {"findings", "reason"} \
                or not isinstance(declared.get("findings", []), list) \
                or not isinstance(declared.get("reason") or "", str) \
                or not all(isinstance(x, str) and x.strip() for x in declared.get("findings") or []):
            raise AmendError("motivated_by is a mapping of findings (a list of finding ids) and reason (text)")
        mine = [x.strip() for x in declared.get("findings") or []]
        reason = " ".join(str(declared.get("reason") or "").split()) or None
        if not mine and not reason:
            raise AmendError("motivated_by names no finding: give the reason none did")
    ids = list(dict.fromkeys(ids + mine))
    led = _ledger_rows(parent) if ids else {}
    if mine and led is None:
        raise AmendError("motivated_by names findings but the parent has no findings.jsonl")
    cats, out = (_categories(parent) if ids else {}), []
    for i in ids:
        r = (led or {}).get(i)
        if r is None and i in mine:
            raise AmendError(f"motivated_by: {i} is not a finding in the parent's findings.jsonl")
        if r is None:
            out.append({"id": i, "in_ledger": False})
            continue
        out.append({"id": i, "in_ledger": True, **{k: r.get(k) for k in _MOTIVE_FIELDS},
                    "category": cats.get((r.get("source"), i)),
                    "fingerprint": FL.answer_fingerprint(i, kind=r.get("kind"), fix_requires=r.get("fix_requires"),
                                                         claim=r.get("claim"))})
    return {"schema": MOTIVE, "parent_run": os.path.basename(os.path.abspath(parent)),
            "source": "rethink_request" if request is not None else "patch" if declared is not None else "unstated",
            "request_round": (request or {}).get("round"), "findings": out, "reason": reason}


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
    if signed is None and any(os.path.lexists(os.path.join(parent, f)) for f in (SO.SPEC_SIGNOFF,
                                                                                  "decision_brief.signoff.json")):
        raise AmendError(f"{parent}: a sign-off record is present but binds no readable spec digest")
    if signed and _sha(spec) != signed:
        raise AmendError(f"{parent}: {SPEC} changed after it was signed; amend from the signed spec")
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
    """Copy what the child inherits (lib.safe_io; a link refused; mode and mtime kept: large files hash by mtime)."""
    from lib.safe_io import read_nofollow, write_nofollow
    derived, n, modes = _derived_paths(), 0, []

    def unreadable(e):
        raise AmendError(f"the parent cannot be read in full: {e}")
    for dp, dirs, fns in os.walk(parent, onerror=unreadable):      # every entry, before anything is left out
        for x in dirs + fns:
            if os.path.islink(os.path.join(dp, x)):
                raise AmendError(f"{os.path.relpath(os.path.join(dp, x), parent)}: a link in the parent run, refused")
    for dp, dirs, fns in os.walk(parent, onerror=unreadable):
        rel_dir = os.path.relpath(dp, parent)
        if rel_dir != ".":
            os.makedirs(os.path.join(stage, rel_dir), mode=0o700, exist_ok=True)
            modes.append((os.path.join(stage, rel_dir), os.stat(dp).st_mode & 0o777))
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git")
                         and not _skip(os.path.normpath(os.path.join(rel_dir, d)).replace(os.sep, "/"), derived))
        for f in sorted(fns):
            rel = os.path.normpath(os.path.join(rel_dir, f)).replace(os.sep, "/")
            if _skip(rel, derived):
                continue
            data, st = read_nofollow(parent, rel)
            write_nofollow(stage, rel, data, exclusive=True)
            os.chmod(os.path.join(stage, rel), st.st_mode & 0o777)     # a private input stays private
            os.utime(os.path.join(stage, rel), ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
            n += 1
    for d, m in reversed(modes):   # after the files: a read-only parent directory still receives its copies
        os.chmod(d, m)
    return n


# ------------------------------------------------------------------ the checks
def _job_keyed(child: str, spec_path: str) -> dict:
    """{file: (rows, reusable or None, rule)}: trusted row files reused by job (joint calibration, row_reuse), each
    job counted once when its engine identity and adapter digest are current; other files: compute.py's rule."""
    from toolkit.analysis import joint_calibration as JC, row_reuse as RR
    try:
        from toolkit.engines.identity import engine_identity
        from toolkit.spec import schema
        ident = engine_identity(schema.load(spec_path).engine())
    except Exception:  # noqa: BLE001 -- engine: auto is chosen after SIGN: rows are listed, not counted
        ident = None
    adapter = JC.adapter_digest()
    legacy = os.environ.get(JC.LEGACY_ADAPTER_ENV) == adapter
    got, files = {}, []
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
                files.append((os.path.relpath(f, child), rows, fad, "joint calibration" if jc else "row_reuse"))
    good = {sha for _, rows, fad, _ in files for sha, b in rows if ident is not None
            and b.get("provenance", {}).get("engine") == ident and (fad == adapter or (fad is None and legacy))}
    seen, todo = set(), set()
    for rel, rows, fad, rule in files:          # a job counted once, whichever file or archive holds it
        shas = {sha for sha, _ in rows}
        new_ok, new_todo = (shas & good) - seen, (shas - good) - todo
        seen, todo = seen | new_ok, todo | new_todo
        got[rel] = (len(rows), None if ident is None else len(new_ok), rule, len(new_todo))
    return got


def reuse(child: str, spec_path: str) -> dict:
    """Per stored batch: rows reusable under the current rules, rows to compute, which rule decides, and why."""
    out = {"rule": "job-keyed batches: current engine identity and adapter digest; others: compute.py's code "
                   "fingerprint. A row answers only the same job: reusable rows are eligible, not promised"}
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
            if b["out"] not in jk and re.fullmatch(r"analysis/joint_calibration/call\d+/(points|results)\.jsonl", b["out"]):
                row.update(reusable=0, rule="job-keyed (joint calibration)",
                           why="no trusted digest: the calibration does not read this file")
            elif b["out"] in jk:
                n, ok_, rule, todo = jk[b["out"]]
                row.update(stored=n, reusable=ok_ or 0, rule=f"job-keyed ({rule})", to_compute=todo,
                           why="engine identity and adapter digest current" if ok_ == n else (
                               "engine chosen after SIGN: not counted" if ok_ is None
                               else "engine identity or adapter digest changed for some rows"))
            row.setdefault("to_compute", row["stored"] - row["reusable"])
            out["batches"].append(row)
        listed = {b["out"] for b in out["batches"]}
        for f, (n, ok_, rule, todo) in sorted(jk.items()):       # e.g. a merged results file with its sidecar
            if f not in listed:
                out["batches"].append({"out": f, "fn": None, "stored": n, "reusable": ok_ or 0,
                                       "to_compute": todo, "rule": f"job-keyed ({rule})",
                                       "why": "trusted by its reuse sidecar"})
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
    led = os.path.lexists(os.path.join(parent, "findings.jsonl"))     # a parent stopped before CRITIQUE has none
    r = subprocess.run([sys.executable, mod, "import", child, "--critique", *(["--parent", parent] if led else [])],
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


def _guards(rd: str, raw: dict) -> list:
    """What must hold before anything reads the patched spec's files or builds its engine (Codex r3 on #342): every
    cited path inside the run (h_SPEC's record_cited_inputs) and the named engine's signed configuration within its
    limits (analysis_backend.signed_config_problems)."""
    if os.path.join(ROOT, "agents") not in sys.path:
        sys.path.insert(0, os.path.join(ROOT, "agents"))
    import analysis_backend as AB
    import stage_machine as SM
    return list(SM.record_cited_inputs(rd)) + list(AB.signed_config_problems(raw))


def _pre_sign(rd: str, derived) -> tuple:
    """(problems, candidate rows): the backend's pre-sign checks and the geometry check, as h_SPEC runs them."""
    if os.path.join(ROOT, "agents") not in sys.path:
        sys.path.insert(0, os.path.join(ROOT, "agents"))
    import analysis_backend as AB
    from toolkit.spec import geometry as GEO
    b = AB.backend()
    import stage_machine as SM
    errs = list(SM.record_cited_inputs(rd))
    errs += list(b.pre_sign_problems(rd, derived=True) if derived else b.pre_sign_problems(rd))
    errs += [f"{x['code']}: {x['claim']}" for x in GEO.problems(GEO.run(rd))]
    return errs, getattr(b, "candidates", None)


def _ready(ds: dict, rc: dict, child: str, mode: str) -> dict:
    """The dry-sign, failed when the condition import failed or a lead would wait for the owner at SIGN."""
    from toolkit.spec import geometry as GEO
    if rc.get("status") == "failed":
        return dict(ds, ok=False, error=f"review conditions not imported: {rc.get('output', '')[-500:]}")
    pend = GEO.owner_pending(GEO.run(child)) if mode == "lead" else []
    return dict(ds, ok=False, error=f"geometry exceptions wait for the owner: {pend[:5]}") if pend else ds


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
    return _sha(open(os.path.join(d, rel), "rb").read()) if os.path.isfile(os.path.join(d, rel)) else "none"


def amend(parent, patch=None, from_request=False, out=None, signoff=None, repo=ROOT, log=print) -> dict:
    """Make the child run (module docstring) and return its amendment record. AmendError: refused, no child."""
    import yaml
    from lib import pipeline_id
    from lib.safe_io import write_nofollow
    from toolkit.spec import evidence_dossier as ED, schema, signoff as SO
    parent = os.path.realpath(parent)
    st, st_sha, spec_bytes, signed = _parent(parent)
    request = _request(parent, signed) if from_request else None
    if request and request.get("findings") and not os.path.isfile(os.path.join(parent, "findings.jsonl")):
        raise AmendError("the request names findings but the parent has no findings.jsonl to carry them")
    p = load_patch(patch, request)
    why = motivation(parent, request, p.get("motivated_by"), required=from_request)    # before anything is made
    _no_aliases(spec_bytes, SPEC)
    spec0 = yaml.safe_load(spec_bytes.decode("utf-8")) or {}
    spec1, changes = apply_ops(spec0, p["spec"], SPEC) if p.get("spec") else (spec0, [])
    drel = ED.dossier_reference(spec1)
    dossier = None
    if p.get("dossier"):
        if not drel or not os.path.isfile(os.path.join(parent, drel)):
            raise AmendError("dossier operations, but the spec names no evidence dossier in the parent")
        dbytes = ED.read_dossier(parent, drel)
        _no_aliases(dbytes, drel)
        d0 = yaml.safe_load(dbytes.decode("utf-8")) or {}
        d1, dch = apply_ops(d0, p["dossier"], drel)
        if d1 != d0:
            dossier = _dump(d1, dbytes, f"Amended by {PRODUCER} ({p.get('id') or 'unnamed'})")
        changes += dch
    if spec1 == spec0 and dossier is None:
        raise AmendError("the patch changes nothing")
    label, psha = str(p.get("id") or "unnamed"), patch_sha256(p)     # before anything is made
    reason1 = " ".join(p["reason"].split())
    new_spec = _dump(spec1, spec_bytes, f"Amendment {label} by {PRODUCER} from parent spec sha256 "
                                        f"{signed or _sha(spec_bytes)}: {reason1}")
    child = os.path.realpath(out) if out else _next_child(parent)
    if os.path.lexists(child):
        raise AmendError(f"{child} exists")
    pst, up = os.stat(parent), os.path.dirname(child)
    while not os.path.exists(up):
        up = os.path.dirname(up)
    while True:                    # by inode: a case-insensitive spelling of the parent is the parent
        if os.path.samestat(os.stat(up), pst):
            raise AmendError("the child cannot be inside the parent run")
        if os.path.dirname(up) == up:
            break
        up = os.path.dirname(up)
    mode = signoff or st.get("signoff")
    if mode not in ("owner", "lead"):
        mode = "owner"             # a parent run with --signoff none: its child is signed, by the owner by default
    stage = child + ".partial"
    os.mkdir(stage, os.stat(parent).st_mode & 0o777)
    try:
        n = _copy_tree(parent, stage)
        for rel, data in ((SPEC, new_spec), *(((drel, dossier),) if dossier is not None else ())):
            mode0 = os.stat(os.path.join(stage, rel)).st_mode & 0o777
            write_nofollow(stage, rel, data, atomic=True)
            os.chmod(os.path.join(stage, rel), mode0)
        errs = _guards(stage, spec1)       # before the schema reads a cited file or builds the engine (Codex r3)
        if not errs:
            errs = list(schema.validate(schema.load(os.path.join(stage, SPEC), data=new_spec), pending_ok=True))
        if not errs:               # what h_SPEC checks before SIGN (backend pre-sign checks, geometry)
            errs, cands = _pre_sign(stage, st.get("derived_inputs"))
        if errs:
            raise AmendError("the amended spec does not validate: " + "; ".join(map(str, errs[:10])))
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    os.rename(stage, child)
    pin = pipeline_id.code_pin(repo)
    meta = {"pipeline": "v2", "question": st.get("question"), "signoff": mode, "max_rounds": st.get("max_rounds"),
            "parent_run": {"run_dir": parent, "run_state_sha256": st_sha}, "parent_spec_sha256": signed,
            "parent_spec_signed": bool(signed), "code_pin": pin, "amendment": label, "patch_sha256": psha,
            "producer": PRODUCER, "created": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "motivated_by": why}
    write_nofollow(child, "metadata.json", json.dumps(meta, indent=2).encode(), atomic=True)
    rounds = st.get("max_rounds") if type(st.get("max_rounds")) is int and st["max_rounds"] > 0 else 3
    keep = ("question", "gate_mode", "budget_usd", "task_cap_usd", "v2_rules", "derived_inputs",
            "derive_unsandboxed", "world_model_stages")
    cst = {**{k: st[k] for k in keep if k in st}, "max_rounds": rounds, "state": "SIGN", "round": 1, "signoff": mode, "spent_usd": 0.0,
           "paused": "awaiting_signoff", "status": None, "tries": {}, "journal": {},
           "history": [{"at": meta["created"], "from": None, "to": "SIGN",
                        "why": f"amendment {label} of {parent} ({len(changes)} change(s)): {reason1}"[:500]}]}
    rc = _review_conditions(child, parent)
    _, cands = _pre_sign(child, st.get("derived_inputs"))   # the geometry record and candidates, at the final path
    SO.write_sheet(child, cands)
    expect = {"expect_sha256": _file_sha(child, SPEC), "expect_sheet_sha256": _file_sha(child, SO.SHEET),
              "expect_derivations_sha256": _file_sha(child, SO.DERIVATIONS_RECORD)}
    q = shlex.quote(child)
    commit = pin.get("commit", "unknown").removesuffix("-dirty")
    budget = (f" --max-budget-usd {float(st['budget_usd']):g}" if isinstance(st.get("budget_usd"), (int, float))
              and not isinstance(st.get("budget_usd"), bool) else "")
    commands = {
        "owner_sign": f"python3 agents/stage_machine.py sign {q} --by owner --expect-sha256 {expect['expect_sha256']} "
                      f"--expect-sheet-sha256 {expect['expect_sheet_sha256']} "
                      f"--expect-derivations-sha256 {expect['expect_derivations_sha256']}",
        "launch": f"python3 main.py --v2 --resume {q} --signoff {mode} "
                  f"--max-rounds {rounds}{budget}",
        "smoke": f"python3 scripts/smoke_check.py {q} --pipeline v2 --commit {shlex.quote(commit)} "
                 "--expect 'metadata.json:parent_run' --expect 'metadata.json:code_pin' "
                 "--expect 'amendment_record.json:reuse' --expect 'amendment_record.json:motivated_by' "
                 "--expect 'metadata.json:motivated_by' --expect 'run_state.json~spec signed by' "
                 "--expect 'run_state.json~\"to\": \"EVIDENCE\"'"}
    rec = {"schema": "amendment_record/1", "producer": PRODUCER, "child": child, "id": label, "reason": p["reason"],
           "parent_run": meta["parent_run"], "parent_spec_sha256": signed, "files_copied": n,
           "request": ({k: request.get(k) for k in ("round", "requested_at", "briefs", "findings")}
                       if request is not None else None),
           "changes": changes, "spec_sha256": expect["expect_sha256"], "sheet_sha256": expect["expect_sheet_sha256"],
           "derivations_sha256": expect["expect_derivations_sha256"], "spec_critique": _critique(child, spec1),
           "review_conditions": rc, "dry_sign": _ready(dry_sign(child, mode, expect), rc, child, mode),
           "reuse": reuse(child, os.path.join(child, SPEC)), "code_pin": pin, "signoff": mode, "commands": commands,
           "motivated_by": why}
    rec["run_state"] = cst
    write_nofollow(child, RECORD, json.dumps(rec, indent=1, default=str).encode(), atomic=True)
    if rec["dry_sign"]["ok"]:      # last, and only when it would sign: otherwise nothing can resume the child
        write_nofollow(child, STATE, json.dumps(cst, indent=1).encode(), atomic=True)
    _print(rec, log)
    return rec


def finish(child: str, log=print) -> dict:
    """A kept child whose dry-sign failed (open review conditions, say): the sheet regenerated with the candidates,
    the dry-sign and commands redone, and the run state written once it would sign."""
    from lib.safe_io import read_nofollow, write_nofollow
    from toolkit.spec import signoff as SO
    child = os.path.realpath(child)
    try:
        rec = json.loads(read_nofollow(child, RECORD)[0])
        cst = rec["run_state"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise AmendError(f"{child}: no amendment record to finish ({e})") from None
    if os.path.lexists(os.path.join(child, STATE)):
        raise AmendError(f"{child} already has its run state")
    rc = _review_conditions(child, rec["parent_run"]["run_dir"])
    errs, cands = _pre_sign(child, cst.get("derived_inputs"))
    if errs:
        raise AmendError("the child fails the pre-sign checks: " + "; ".join(errs[:10]))
    SO.write_sheet(child, cands)
    expect = {"expect_sha256": _file_sha(child, SPEC), "expect_sheet_sha256": _file_sha(child, SO.SHEET),
              "expect_derivations_sha256": _file_sha(child, SO.DERIVATIONS_RECORD)}
    rec.update(review_conditions=rc, dry_sign=_ready(dry_sign(child, rec["signoff"], expect), rc, child,
                                                     rec["signoff"]),
               spec_sha256=expect["expect_sha256"], sheet_sha256=expect["expect_sheet_sha256"],
               derivations_sha256=expect["expect_derivations_sha256"])
    rec["commands"]["owner_sign"] = (f"python3 agents/stage_machine.py sign {shlex.quote(child)} --by owner "
                                     + " ".join(f"--{k.replace('_', '-')} {v}" for k, v in expect.items()))
    write_nofollow(child, RECORD, json.dumps(rec, indent=1, default=str).encode(), atomic=True)
    if rec["dry_sign"]["ok"]:
        write_nofollow(child, STATE, json.dumps(cst, indent=1).encode(), atomic=True)
    _print(rec, log)
    return rec


def _print(rec: dict, log) -> None:
    short = lambda v: (json.dumps(v, default=str)[:160] if v is not None else "(absent)")  # noqa: E731
    log(f"Child run: {rec['child']} (amendment {rec['id']}; parent spec {rec['parent_spec_sha256']}). Changes:")
    for c in rec["changes"]:
        log(f"  {c['op']} {c['file']}:{c['path']}: {short(c['before'])} -> {short(c['after'])}")
    sc, r = rec["spec_critique"], rec["reuse"]
    log(f"Spec critique: {sc.get('high')} HIGH, {sc.get('medium')} MEDIUM, {sc.get('declared')} declared "
        f"{sc.get('error') or ''}; review conditions: {rec['review_conditions']['status']}")
    log(f"Reuse: {r.get('reusable')} of {r.get('stored')} stored rows reusable {r.get('error') or ''}")
    for b in r.get("batches", []):
        log(f"  {b['out']}: {b['reusable']} reusable, {b['to_compute']} to compute; {b['rule']}: {b['why']}")
    pin, ds = rec["code_pin"], rec["dry_sign"]
    log(f"Code pin: {pin.get('commit')} (clean: {pin.get('clean')}, on a remote: {pin.get('on_remote')})")
    if not ds["ok"]:
        log(f"Dry-sign as {ds['by']} FAILED on a scratch copy: {ds.get('error') or ds.get('status')}\nNo sign command "
            "and no run state. Open review conditions: dispose of them, then python3 -m toolkit.spec.amend --finish "
            f"{shlex.quote(rec['child'])}. Otherwise fix the patch and amend again.")
        return
    log(f"Dry-sign as {ds['by']}: a scratch copy signed and verified.")
    log(("Owner sign command" if rec["signoff"] == "owner" else "Lead mode: the lead signs at SIGN on resume; "
         "the owner may sign first") + ":\n  " + rec["commands"]["owner_sign"])
    log(f"Launch, after signing, from a checkout at {pin.get('commit')}:\n  " + rec["commands"]["launch"])
    log("Smoke markers:\n  " + rec["commands"]["smoke"])


def _self_test() -> int:
    import yaml
    import compute
    from lib import pipeline_id
    from toolkit.spec import schema, signoff as SO
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        fails.extend([] if c else [m])

    def tree(d):
        return _sha(json.dumps(sorted((os.path.relpath(os.path.join(dp, f), d), sha(os.path.join(dp, f)))
                                      for dp, _, fns in os.walk(d) for f in fns)).encode())

    def sha(p):
        return _sha(open(p, "rb").read())

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
        tsp = schema.toy_spec(par)
        raw = yaml.safe_load(open(tsp))     # the toy's two critique findings declared: no open review condition
        raw["principle_departures"] = [{"principle": 2, "check": "headline_comparator", "reason": "toy"},
                                       {"principle": 3, "check": "baseline_burden", "reason": "toy"}]
        yaml.safe_dump(raw, open(tsp, "w"), sort_keys=False)
        _pre_sign(par, False)              # what h_SPEC records before SIGN (data/inputs_provenance.json)
        SO.write_sheet(par)
        SO.sign(par, "owner")
        psig = SO.signed_sha(par)
        w(par, "run_state.json", json.dumps({"question": "q", "state": "FINAL", "status": "completed_gate_not_accepted",
                                             "signoff": "owner", "max_rounds": 2, "budget_usd": 5.0, "spent_usd": 3.0,
                                             "derived_inputs": False, "history": [{"to": "FINAL"}]}))
        for rel in ("report.md", "critique_domain.yaml", "findings.jsonl", "analysis/manifest.json",
                    "analysis/point.yaml", "figures/a.png", "engine_choice.json"):
            w(par, rel, "stale\n")
        w(par, "findings.jsonl", "")       # a readable ledger with no open finding (review_conditions --parent)
        w(par, "data/derived.csv", "u,v\n1,2\n")
        os.chmod(os.path.join(par, "data", "derived.csv"), 0o600)

        w(par, "analysis/benchmarks_at_spec_values.record.json", "{}\n")
        w(par, "models/fn.py", "def f(p):\n    return {'y': p['x']}\n")
        w(par, "gone.py", "def f(p):\n    return {'y': 2 * p['x']}\n")
        for fn, out in (("models/fn.py::f", "analysis/batch/points.jsonl"), ("gone.py::f", "analysis/b2/points.jsonl")):
            compute.run_map(par, fn, [{"x": i} for i in range(3)], os.path.join(par, out), backend="local",
                            max_workers=1, ledger=False)
        os.remove(os.path.join(par, "gone.py"))
        p_rows = compute.reuse_report(par)
        for rel in ("done_when_registry.json", "fix_progress_r1.json", "scope_declaration.yaml",
                    "models/decision_tests/DT1.json"):      # written by the run after its batches
            w(par, rel, "{}\n")
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
                            "decision_brief.signed", "data/geometry_signoff.json", "done_when_registry.json",
                            "fix_progress_r1.json", "scope_declaration.yaml", "models/decision_tests")
                if os.path.lexists(os.path.join(child, x))]
        kept = [x for x in ("data/derived.csv", "analysis/benchmarks_at_spec_values.record.json", "models/fn.py",
                            "analysis/batch/points.jsonl.summary.json", "units.csv")
                if not os.path.exists(os.path.join(child, x))]
        ok(not gone and not kept, f"AM_CHILD_CONTENT run outputs and sign-off records left out ({gone}); inputs and "
                                  f"stored rows kept ({kept})")
        ok(os.stat(os.path.join(child, "data", "derived.csv")).st_mode & 0o777 == 0o600,
           "AM_MODE_KEPT a private input stays private in the child")
        ok(cst.get("state") == "SIGN" and cst.get("paused") == "awaiting_signoff" and cst.get("signoff") == "owner"
           and cst.get("spent_usd") == 0 and cst.get("budget_usd") == 5.0 and cst.get("max_rounds") == 2,
           f"AM_STATE the child waits at SIGN for the owner ({cst})")
        src = SO.sheet_sources(open(os.path.join(child, SO.SHEET)).read()) or {}
        ssha, shsha = sha(os.path.join(child, "decision_spec.yaml")), sha(os.path.join(child, SO.SHEET))
        cmd = rec.get("commands", {}).get("owner_sign", "")
        ok(src.get("spec_sha256") == ssha and rec.get("dry_sign", {}).get("ok") is True
           and f"--expect-sha256 {ssha}" in cmd and f"--expect-sheet-sha256 {shsha}" in cmd
           and SO.status(child) == "awaiting",
           f"AM_SHEET_MATCHES_SPEC the sheet shows the child spec; a copy dry-signs; the child stays unsigned ({cmd}, "
           f"{rec.get('dry_sign')})")
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
        row1 = json.dumps({"id": "D-1", "kind": "critique_blocker", "severity": "MEDIUM", "source": "critique-domain",
                           "status": "open", "owner_state": "SPEC", "claim": "c", "fix_requires": "r"}) + "\n"
        w(par, "findings.jsonl", row1)     # MEDIUM: no review condition is carried from it
        w(par, "critique_domain.yaml", "blockers:\n- {id: D-1, category: methods}\n")
        rec2 = amend(par, from_request=True, signoff="lead", log=lambda *a: None)
        w(par, "findings.jsonl", "")
        w(par, "critique_domain.yaml", "stale\n")
        c2 = rec2.get("child", "")
        sys.path.insert(0, os.path.join(ROOT, "agents"))
        import findings_ledger as FL_
        mb2 = rec2.get("motivated_by") or {}
        f2 = (mb2.get("findings") or [{}])[0]
        ok(mb2.get("source") == "rethink_request" and mb2.get("parent_run") == "parent" and mb2.get("request_round") == 1
           and f2.get("in_ledger") is True and f2.get("kind") == "critique_blocker" and f2.get("category") == "METHODS"
           and f2.get("source") == "critique-domain"
           and f2.get("fingerprint") == FL_.answer_fingerprint("D-1", kind="critique_blocker", fix_requires="r", claim="c")
           and json.load(open(os.path.join(c2, "metadata.json"))).get("motivated_by") == mb2,
           f"AM_MOTIVE_FROM_REQUEST metadata and record name the parent's motivating finding and the parent ({mb2})")
        ok((rec.get("motivated_by") or {}).get("source") == "unstated" and rec["motivated_by"]["findings"] == []
           and meta.get("motivated_by") == rec["motivated_by"],
           f"AM_MOTIVE_UNSTATED a manual patch naming no finding is recorded as unstated ({rec.get('motivated_by')})")
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
        w(par, "findings.jsonl", row1)
        w(par, "spec_rethink_request.json", json.dumps(dict(rq, findings=[], briefs=[{"task": "T1", "findings": [],
                                                                                     "brief": "b"}],
                                                       patch=dict(rq["patch"], motivated_by={"findings": ["D-1"]}))))
        refused(lambda: amend(par, from_request=True, log=lambda *a: None),
                "AM_MOTIVE_REQUIRED a rethink whose request names no finding (a patch's declaration is no substitute)")
        # Codex r2 of #373: a finding id is a non-empty string; [] or a number or blank text is refused, never coerced
        for name, fs_, bf in (("id []", [{"id": []}], []), ("id blank", [{"id": " "}], []), ("id a number", [{"id": 5}],
                              []), ("a brief's id []", [], [[]]), ("a finding without id", [{"claim": "c"}], [])):
            w(par, "spec_rethink_request.json", json.dumps(dict(rq, findings=fs_, briefs=[{"task": "T1", "findings": bf,
                                                                                         "brief": "b"}])))
            refused(lambda: amend(par, from_request=True, log=lambda *a: None), f"AM_MOTIVE_ID_REFUSED {name}")
        os.remove(os.path.join(par, "spec_rethink_request.json"))
        w(par, "critique_domain.yaml", "blockers: 1\n")      # malformed but descriptive: no category, no refusal
        env = {"reason": "r", "spec": [{"set": "constraints.envelope_usd", "value": 9300}]}
        yaml.safe_dump(dict(env, motivated_by={"findings": ["D-1"]}), open(pf, "w"))
        rec6 = amend(par, pf, log=lambda *a: None)
        m6 = rec6.get("motivated_by") or {}
        ok(m6.get("source") == "patch" and [f.get("id") for f in m6.get("findings") or []] == ["D-1"]
           and m6["findings"][0].get("in_ledger") is True and m6["findings"][0].get("fingerprint"),
           f"AM_MOTIVE_DECLARED a manual patch names its motivating finding from the parent's ledger ({m6})")
        shutil.rmtree(rec6["child"])
        w(par, "critique_domain.yaml", "stale\n")
        for name, mb in (("unknown id", {"findings": ["D-9"]}), ("no finding and no reason", {}),
                         ("a blank id", {"findings": [" "]}), ("findings false", {"findings": False, "reason": "m"}),
                         ("findings not a list", {"findings": "D-1"}), ("another key", {"why": "x"})):
            yaml.safe_dump(dict(env, motivated_by=mb), open(pf, "w"))
            refused(lambda: amend(par, pf, log=lambda *a: None), f"AM_MOTIVE_REFUSED {name}")
        w(par, "findings.jsonl", "")
        n_dirs = sorted(os.listdir(td))
        bad = {"unresolved": {"reason": "r", "spec": [{"set": "constraints.nope", "value": 1}]},
               "two ops": {"reason": "r", "spec": [{"set": "constraints.envelope_usd", "remove": "x", "value": 1}]},
               "no reason": {"spec": [{"set": "constraints.envelope_usd", "value": 1}]},
               "no-op": {"reason": "r", "spec": [{"set": "constraints.envelope_usd", "value": 9000}]},
               "add existing": {"reason": "r", "spec": [{"add": "constraints.envelope_usd", "value": 1}]},
               "ambiguous selector": {"reason": "r", "spec": [{"remove": "analysis.decision_tests[id=DT9]"}]},
               "invalid spec": {"reason": "r", "spec": [{"set": "comparator.package", "value": "nope"}]},
               "not a mapping": ["x"],
               "multiline id": {"id": "A\nextra: 1\n#", "reason": "r", "spec": [{"set": "constraints.envelope_usd",
                                                                                 "value": 1}]},
               "selector on a missing key": {"reason": "r", "spec": [{"remove": "options.cost_items[unit=None]"}]}}
        for name, p in bad.items():
            yaml.safe_dump(p, open(pf, "w"))
            refused(lambda: amend(par, pf, log=lambda *a: None), f"AM_BAD_PATCH_REFUSED {name}")
        ok(sorted(os.listdir(td)) == n_dirs and tree(par) == before,
           "AM_BAD_PATCH_REFUSED leaves no child and the parent unchanged")
        # Codex r3 on #342 (P1-3): a patch naming a file outside the run, or an engine configuration over its limit, is
        # refused before the schema reads the file or builds the engine
        import analysis_backend as AB_
        w(td, "outside/units.csv", open(os.path.join(par, "units.csv")).read())
        real_validate, seen, n0 = schema.validate, [], sorted(os.listdir(td))
        schema.validate = lambda sp, *a, **k: seen.append(sp.raw) or real_validate(sp, *a, **k)
        try:
            for name, op, why in (
                    ("outside", {"set": "decision_units.csv", "value": os.path.join(td, "outside", "units.csv")},
                     "outside the run directory"),
                    ("engine limit", {"set": "engine", "value": {"ref": AB_.EMPIRICAL, "config": {"components": [
                        f"c{i}" for i in range(AB_.EMPIRICAL_MAX_COMPONENTS + 1)]}}}, "at most")):
                yaml.safe_dump({"reason": "r", "spec": [op]}, open(pf, "w"))
                try:
                    amend(par, pf, log=lambda *a: None)
                    got = None
                except AmendError as e:
                    got = str(e)
                ok(got is not None and why in got and not seen and sorted(os.listdir(td)) == n0,
                   f"AM_GUARDS_BEFORE_VALIDATE {name}: refused before the schema reads the patched spec "
                   f"({(got or '')[:120]}; validated {len(seen)})")
                seen.clear()
        finally:
            schema.validate = real_validate
        shutil.rmtree(os.path.join(td, "outside"))
        # Codex r3 of #349: a patch with mixed key types (YAML) has a digest; no half-made child is left
        open(pf, "w").write("reason: r\nspec:\n- set: options.prices.index\n  value: {'2024': 1.0, 2025: 1.03}\n")
        try:
            got = amend(par, pf, log=lambda *a: None).get("child")
        except AmendError:
            got = None
        except Exception as e:  # noqa: BLE001 -- the finding
            got = f"{type(e).__name__}: {e}"
        half = [c for c in sorted(os.listdir(td)) if c.startswith("parent.amend")
                and not os.path.isfile(os.path.join(td, c, "metadata.json"))]
        ok((got is None or os.path.isdir(str(got))) and not half,
           f"AM_PATCH_DIGEST_MIXED_KEYS a YAML patch with mixed key types leaves no half-made child ({got}, {half})")
        if got and os.path.isdir(str(got)):
            shutil.rmtree(got)
        yaml.safe_dump({"reason": "r", "spec": [{"set": "constraints.envelope_usd", "value": 9200}]}, open(pf, "w"))
        st = json.load(open(os.path.join(par, "run_state.json")))
        w(par, "run_state.json", json.dumps(dict(st, state="ANALYZE", status=None, paused=None)))
        refused(lambda: amend(par, pf, log=lambda *a: None), "AM_RUNNING_PARENT_REFUSED a parent neither final nor paused")
        w(par, "run_state.json", json.dumps(st))
        os.symlink(os.path.join(td, "a1.yaml"), os.path.join(par, "data", "link.yaml"))
        refused(lambda: amend(par, pf, log=lambda *a: None), "AM_LINK_REFUSED a parent holding a link")
        os.remove(os.path.join(par, "data", "link.yaml"))
        refused(lambda: amend(par, pf, out=os.path.join(par, "figures", "c"), log=lambda *a: None),
                "AM_OUT_INSIDE_PARENT_REFUSED a child inside the parent")
        refused(lambda: _no_aliases(b"a: &v {x: 1}\nb: *v\n", "spec"), "AM_ALIAS_REFUSED a spec with YAML aliases")
        sp_ = os.path.join(par, "decision_spec.yaml")
        keep_ = open(sp_, "rb").read()
        open(sp_, "ab").write(b"# edited after signing\n")
        refused(lambda: amend(par, pf, log=lambda *a: None), "AM_CHANGED_AFTER_SIGN_REFUSED the working spec is not the signed one")
        open(sp_, "wb").write(keep_)
        w(par, "run_state.json", json.dumps(dict(st, signoff="none")))
        rec3 = amend(par, pf, log=lambda *a: None)
        ok(json.load(open(os.path.join(rec3["child"], "run_state.json")))["signoff"] == "owner",
           "AM_NONE_PARENT_OWNER_CHILD a parent run without sign-off gives a child the owner signs")
        w(par, "run_state.json", json.dumps(st))
        shutil.rmtree(rec3["child"])
        # Codex round 2
        d_, _ = apply_ops({"r": {"01": 2, "1": 3}, "n": {1: 4}}, [{"set": "r.01", "value": 9}, {"set": "n.1", "value": 5}], "x")
        ok(d_ == {"r": {"01": 9, "1": 3}, "n": {1: 5}}, f"AM_NUMERIC_KEY a text key keeps its spelling ({d_})")
        v_ = {"cost": 1}
        d_, _ = apply_ops({"a": [], "b": []}, [{"add": "a", "value": v_}, {"add": "b", "value": v_},
                                              {"set": "b.0.cost", "value": 2}], "x")
        ok(d_["a"][0]["cost"] == 1, f"AM_PATCH_ALIAS_COPIED one patch value inserted twice is two values ({d_})")
        so_ = os.path.join(par, SO.SPEC_SIGNOFF)
        keep_so = open(so_, "rb").read()
        open(so_, "wb").write(keep_so[:10])
        refused(lambda: amend(par, pf, log=lambda *a: None), "AM_UNREADABLE_SIGNOFF_REFUSED a truncated sign-off record")
        open(so_, "wb").write(keep_so)
        os.makedirs(os.path.join(par, "figures", "sub"), exist_ok=True)
        os.symlink(pf, os.path.join(par, "figures", "sub", "l"))
        refused(lambda: amend(par, pf, log=lambda *a: None), "AM_PRUNED_LINK_REFUSED a link in a directory left out")
        shutil.rmtree(os.path.join(par, "figures", "sub"))
        os.makedirs(os.path.join(par, "data", "private"))
        w(par, "data/private/x.csv", "a\n")
        os.chmod(os.path.join(par, "data", "private"), 0o700)
        # a patch that withdraws a declaration: an open review condition, no run state, then --finish
        yaml.safe_dump({"reason": "r", "spec": [{"remove": "principle_departures[check=baseline_burden]"}]}, open(pf, "w"))
        rec4 = amend(par, pf, log=lambda *a: None)
        c4 = rec4["child"]
        ok(os.stat(os.path.join(c4, "data", "private")).st_mode & 0o777 == 0o700,
           "AM_DIR_MODE_KEPT a private directory stays private")
        ok(rec4["dry_sign"]["ok"] is False and not os.path.exists(os.path.join(c4, "run_state.json")),
           f"AM_FAILED_DRY_SIGN_NO_STATE an open condition: no run state, nothing resumes ({rec4['dry_sign']})")
        rcf = os.path.join(c4, "review_conditions.yaml")
        rows = yaml.safe_load(open(rcf))
        for r_ in rows["conditions"]:
            r_.update(disposition="deferred", owner="owner", due="2099-01-01")
        yaml.safe_dump(rows, open(rcf, "w"))
        rec5 = finish(c4, log=lambda *a: None)
        ok(rec5["dry_sign"]["ok"] is True and json.load(open(os.path.join(c4, "run_state.json")))["state"] == "SIGN"
           and f"--expect-sheet-sha256 {sha(os.path.join(c4, SO.SHEET))}" in rec5["commands"]["owner_sign"],
           f"AM_FINISH after the dispositions the child gets its sheet, sign command and run state ({rec5['dry_sign']})")
        shutil.rmtree(c4)
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
    ap.add_argument("parent_run", help="the parent run (with --finish: the kept child)")
    ap.add_argument("--finish", action="store_true", help="re-check a kept child whose dry-sign failed")
    ap.add_argument("--patch", default=None, help="the amendment (YAML: id, reason, spec, dossier)")
    ap.add_argument("--from-request", action="store_true", help=f"take the why (and a patch) from {REQUEST}")
    ap.add_argument("--out", default=None, help="the child run directory (default <parent>.amend<k>)")
    ap.add_argument("--signoff", choices=["owner", "lead"], default=None, help="default: the parent's mode")
    a = ap.parse_args(argv)
    try:
        rec = finish(a.parent_run) if a.finish else amend(a.parent_run, a.patch, a.from_request, a.out, a.signoff)
    except AmendError as e:
        print(f"Refused: {e}", file=sys.stderr)
        return 1
    return 0 if rec["dry_sign"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
