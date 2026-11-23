"""The decision spec's sign-off: the signed hash, the record, sign() and the evidence preconditions.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import analysis_backend as AB

from .common import SIGNOFF_FILE, _now, _put


def spec_signoff_findings(run_dir: str, signoff: str, backend=None) -> list[dict]:
    """decision_spec.yaml must not change after sign-off (non-declarable)."""
    if signoff == "none":
        return []
    rec = _signoff(run_dir)
    if backend is not None and getattr(backend, "sign", None):
        st = backend.signoff_status(run_dir)
        rec = {"spec_sha256": signed_hash(run_dir)} if st == "signed" else (None if st != "changed" else {"spec_sha256": "x"})
    if rec is None:
        return [{"kind": "spec_unsigned", "severity": "HIGH", "stage": "PLAN", "target_stage": "PLAN", "key": "spec",
                 "scope_declarable": False, "claim": "decision_spec.yaml has no sign-off."}]
    if rec.get("spec_sha256") != signed_hash(run_dir):
        return [{"kind": "spec_changed_after_signoff", "severity": "HIGH", "stage": "PLAN", "target_stage": "PLAN",
                 "key": "spec", "scope_declarable": False,
                 "claim": "decision_spec.yaml changed after sign-off. It is the owner's; restore it or ask for a "
                          "new sign-off. Computational settings belong in analysis_settings.yaml."}]
    from toolkit.spec import review_conditions as RC     # W0-G (Codex r2 P1-5): the conditions the signature bound
    bad = RC.problems(run_dir, None, None)
    if bad:
        return [{"kind": "review_conditions_changed_after_signoff", "severity": "HIGH", "stage": "PLAN",
                 "target_stage": "PLAN", "key": "review_conditions", "scope_declarable": False,
                 "claim": "; ".join(bad)[:600]}]
    return []


# ------------------------------------------------------------------ sign-off
def signed_hash(run_dir: str) -> str | None:
    """sha256 of decision_spec.yaml as a file (the owner signs all of it, as workstream B does)."""
    p = os.path.join(run_dir, AB.SPEC_FILE)
    if os.path.islink(p):
        return None
    try:
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _signoff(run_dir: str) -> dict | None:
    try:
        return json.load(open(os.path.join(run_dir, SIGNOFF_FILE)))
    except (OSError, ValueError):
        return None


def sign(run_dir: str, by: str, note: str = "", expect_sha256: str | None = None) -> dict:
    """The spike adapter's sign-off. The spec is read once, never through a link (lib.safe_io); that same content is
    hashed into the record and frozen with its geometry inputs (Codex on 08f96a9)."""
    import yaml
    from lib.safe_io import LinkRefused, read_nofollow
    try:
        data = read_nofollow(run_dir, AB.SPEC_FILE)[0]
    except FileNotFoundError:
        raise ValueError(f"no {AB.SPEC_FILE} to sign") from None
    except (LinkRefused, OSError) as e:
        raise ValueError(f"{AB.SPEC_FILE}: {e}") from None
    rec = {"by": by, "note": note, "at": _now(), "spec_sha256": hashlib.sha256(data).hexdigest()}
    if expect_sha256 is not None and rec["spec_sha256"] != expect_sha256:
        raise ValueError(f"{AB.SPEC_FILE} changed since it was shown; read it again before signing")
    try:
        spec_doc = yaml.safe_load(data.decode("utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        raise ValueError(f"{AB.SPEC_FILE} does not parse: {e}") from None
    from toolkit.spec import preconditions as PC          # every signing path checks the preconditions (#215)
    from toolkit.spec import review_conditions as RC     # W0-G: read once, validated here and bound in the record
    rc_bytes = RC.read_bytes(run_dir)
    pre = PC.problems(run_dir, data, by, review_bytes=rc_bytes)
    if pre:
        raise ValueError("spec preconditions not met: " + "; ".join(pre[:10]))
    # engine parameter values without a basis, or chosen with a held-out benchmark, are refused whoever signs (as in
    # toolkit/spec/signoff.py sign and scripts/decision_brief.py sign)
    from toolkit.spec import engine_parameters as PV
    held = PV.signoff_refusals(spec_doc if isinstance(spec_doc, dict) else {}, run_dir=run_dir)
    if held:
        raise ValueError("spec not signable (engine_parameters): " + "; ".join(held[:10]))
    from toolkit.spec import signoff as SO      # F6 (#218): every registered precondition, recomputed, whoever signs
    ev = SO.sign_preconditions(run_dir, data)  # (the sheet must show this dossier: refused without one when needed)
    rec.update(evidence=ev["record"], evidence_sha256=ev["files"])
    rec[RC.RECORD_KEY] = RC.sha_of(rc_bytes)
    from toolkit.spec import geometry as GEO
    # the geometry declaration and input hashes of this content; the owner's approvals
    GEO.freeze(run_dir, by, spec=spec_doc if isinstance(spec_doc, dict) else {})
    _put(run_dir, SIGNOFF_FILE, json.dumps(rec, indent=1))
    return rec


def evidence_gate(run_dir: str, engine: tuple | None = None, signoff: str | None = None) -> list[str]:
    """F6 (#218): the registered preconditions of a signed run, recomputed from its files (the sign-off record
    compared; the engine chosen in engine_choice.json, or `engine`): checked at ENGINE_CHOICE on the engine picked and
    on every resume past it, before any compute. Empty: they hold. Fail closed on an error."""
    try:
        from toolkit.spec import preconditions as PC
        # signoff none (nobody signs): coverage without a record, the rule the launch preflight applies too
        return PC.problems(run_dir, None, None, mode="unsigned" if signoff == "none" else "resume",
                           engine=engine)          # every check: the benchmark verdict too
    except Exception as e:  # noqa: BLE001
        return [f"the preconditions could not be checked: {type(e).__name__}: {e}"]


def _spec_signed_sha(run_dir: str) -> str | None:
    """The spec sha256 a sign-off record binds (the spec's own record, or a brief's that hashed the spec), or None."""
    import re as _re
    hexd = _re.compile(r"[0-9a-f]{64}")

    def nested(r):
        m = r.get("sha256")
        return m.get(AB.SPEC_FILE) if isinstance(m, dict) else (False if m is not None else None)
    def own(r):        # the spec's own record: spec_sha256 (spike) or the nested map (toolkit); a present but empty
        v = r.get("spec_sha256") or nested(r)          # spec_sha256 with no nested digest is not a binding: unreadable
        return False if v is None and "spec_sha256" in r else v
    for f, get in ((SIGNOFF_FILE, own),
                   ("decision_brief.signoff.json", nested)):
        p = os.path.join(run_dir, f)
        if not os.path.lexists(p):
            continue
        try:
            rec = json.load(open(p))
        except (OSError, ValueError):
            return "unreadable"          # fail closed: an unreadable record still protects the spec (#261 re-review)
        if not isinstance(rec, dict):
            return "unreadable"
        v = get(rec)
        if v is False or (v is not None and not (isinstance(v, str) and hexd.fullmatch(v))):
            return "unreadable"          # a malformed binding is not a signature (narrow Codex review of #272)
        if v:
            return v
    return None


def spec_signed_once(run_dir: str) -> bool:
    """A sign-off record binds the run's spec: it was signed, so only the owner may change it (a brief signed before
    any spec existed does not count: Codex review of #261 P2-5)."""
    return _spec_signed_sha(run_dir) is not None
