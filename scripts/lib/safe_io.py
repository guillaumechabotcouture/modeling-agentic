#!/usr/bin/env python3
"""
Phase 21 β — fail-closed artifact loading for the STAGE 7 gate.

The validator's historical pattern was:

    try:
        doc = yaml.safe_load(open(path)) or {}
    except (yaml.YAMLError, OSError):
        pass                      # ← silent pass: malformed artifact = no finding
    if doc.get("verdict") == "SOME_FAILURE_STRING":
        emit HIGH                 # ← any other string (incl. invented ones) = no finding

Both halves fail OPEN: an unparseable file, a wrong top-level type, a
renamed key, or an invented verdict string (the 2026-05-03_235631 run
shipped `DEGENERATE_FIT_ACKNOWLEDGED` and
`PARAMETERS_IDENTIFIED_BY_CONSTRUCTION`, neither of which any producing
script can emit) all made the corresponding HIGH vanish.

This module inverts the default. `safe_load_mapping()` returns
`(doc, findings)`; every structural problem becomes a HIGH
`<prefix>_invalid` finding, and a verdict outside the producing
script's closed vocabulary becomes a HIGH `<prefix>_unknown_verdict`.
`run_guarded()` turns an unexpected exception inside a `_check_*`
function into a HIGH `validator_check_crashed` finding instead of a
crash (exit 1 with no JSON) or an empty list.

Findings share the validator's rigor-violation shape:
    {"kind", "severity", "stage", "claim", "scope_declarable"}

Usage:
    python3 scripts/lib/safe_io.py --self-test
"""

from __future__ import annotations

import json
import math
import os
import sys
import traceback
from typing import Any, Callable, Iterable, Optional

import yaml


def _finding(kind: str, stage: str, claim: str,
             severity: str = "HIGH",
             scope_declarable: bool = False) -> dict:
    return {
        "kind": kind,
        "severity": severity,
        "stage": stage,
        "claim": claim,
        # Invalid / tampered artifacts are never scope-declarable: the
        # fix (regenerate the artifact with its producing script) is
        # always available inside the pipeline.
        "scope_declarable": scope_declarable,
    }


def _type_name(t: Any) -> str:
    if isinstance(t, tuple):
        return " | ".join(x.__name__ for x in t)
    return t.__name__


def safe_load_mapping(
    path: str,
    kind_prefix: str,
    *,
    stage: str = "ARTIFACT",
    required_keys: Iterable[str] = (),
    expected_types: Optional[dict[str, Any]] = None,
    allowed_verdicts: Optional[Iterable[str]] = None,
    verdict_key: str = "verdict",
    display_path: Optional[str] = None,
) -> tuple[Optional[dict], list[dict]]:
    """Load a YAML mapping, failing closed.

    Returns (doc, findings):
      - file absent              → (None, [])   caller decides missing semantics
      - unreadable / unparseable → (None, [HIGH <prefix>_invalid])
      - top level not a mapping  → (None, [HIGH <prefix>_invalid])
      - required key missing     → (None, [HIGH <prefix>_invalid])
      - key has the wrong type   → (None, [HIGH <prefix>_invalid])
      - verdict not in allowlist → (doc,  [HIGH <prefix>_unknown_verdict])

    `expected_types` maps key → type or tuple of types; it is only checked
    when the key is present (combine with `required_keys` to require it).
    `allowed_verdicts` is compared case-sensitively after str().strip();
    when given, the verdict key is implicitly required.
    """
    shown = display_path or os.path.basename(path)
    if not os.path.exists(path):
        return None, []
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        return None, [_finding(
            f"{kind_prefix}_invalid", stage,
            f"`{shown}` could not be read/parsed ({type(e).__name__}: "
            f"{str(e).splitlines()[0] if str(e) else ''}). Regenerate it "
            f"with its producing script; a malformed artifact cannot "
            f"satisfy the gate.")]
    if not isinstance(doc, dict):
        return None, [_finding(
            f"{kind_prefix}_invalid", stage,
            f"`{shown}` top level must be a mapping, got "
            f"{type(doc).__name__}. Regenerate it with its producing "
            f"script.")]

    req = list(required_keys)
    if allowed_verdicts is not None and verdict_key not in req:
        req.append(verdict_key)
    missing = [k for k in req if k not in doc]
    if missing:
        return None, [_finding(
            f"{kind_prefix}_invalid", stage,
            f"`{shown}` is missing required key(s) {missing} (present: "
            f"{sorted(map(str, doc.keys()))[:12]}). A renamed or absent "
            f"key is treated as invalid, not as 'no findings'.")]

    for key, typ in (expected_types or {}).items():
        if key in doc and not isinstance(doc[key], typ):
            return None, [_finding(
                f"{kind_prefix}_invalid", stage,
                f"`{shown}`: `{key}` must be {_type_name(typ)}, got "
                f"{type(doc[key]).__name__}.")]

    findings: list[dict] = []
    if allowed_verdicts is not None:
        allowed = sorted(set(allowed_verdicts))
        verdict = str(doc.get(verdict_key) if doc.get(verdict_key) is not None
                      else "").strip()
        if verdict not in allowed:
            findings.append(_finding(
                f"{kind_prefix}_unknown_verdict", stage,
                f"`{shown}` has {verdict_key}: {verdict!r}, which is not "
                f"in the producing script's vocabulary {allowed}. "
                f"Hand-edited or invented verdicts are treated as failing; "
                f"re-run the producing script instead of editing its "
                f"output."))
    return doc, findings


def run_guarded(fn: Callable[..., list], *args: Any,
                check_name: Optional[str] = None, **kwargs: Any) -> list:
    """Call a `_check_*` function; convert an unexpected exception into a
    HIGH `validator_check_crashed` finding (never [] and never a crash)."""
    name = check_name or getattr(fn, "__name__", "check")
    try:
        result = fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 — fail closed by design
        tb = traceback.extract_tb(e.__traceback__)
        where = f"{os.path.basename(tb[-1].filename)}:{tb[-1].lineno}" if tb else "?"
        return [_finding(
            "validator_check_crashed", "GATE",
            f"{name} raised {type(e).__name__}: {e} (at {where}). The "
            f"gate fails closed: an artifact shape the check cannot "
            f"handle is a HIGH, not a silent pass. Fix the artifact or "
            f"the check.")]
    if result is None:
        return []
    if not isinstance(result, list):
        return [_finding(
            "validator_check_crashed", "GATE",
            f"{name} returned {type(result).__name__}, expected list.")]
    return result


def finite_number(x: Any) -> Optional[float]:
    """float(x) if x is a finite real number (bool excluded), else None."""
    if isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def inside_dir(root: str, path: str) -> bool:
    """True when `path`, with every symlink resolved, lies inside `root`
    (also resolved). Checked before opening a file a run names, so a cited
    path or a symlinked parent cannot make a gate read outside the run."""
    try:
        r, p = os.path.realpath(root), os.path.realpath(path)
        return os.path.commonpath([r, p]) == r
    except ValueError:      # different drives
        return False


# Phase 43 γ: two lenient readers that were copied verbatim across scripts
# (coherence_audit / identifiability; lessons / run_autopsy). They are not
# gate loaders: they return None / [] rather than findings, as before.

def sha256_file(path: str) -> Optional[str]:
    """Hex SHA-256 of the file's bytes, or None if it cannot be read."""
    import hashlib
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def read_jsonl(path):
    """Parsed JSON lines of `path`; unparsable lines are skipped and an
    unreadable file gives []."""
    out = []
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

def _self_test() -> int:
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    def write(d: str, name: str, text: str) -> str:
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    with tempfile.TemporaryDirectory() as d:
        # S1: absent file → (None, [])
        doc, fs = safe_load_mapping(os.path.join(d, "nope.yaml"), "x")
        ok(doc is None and fs == [], "S1 absent file is silent")

        # S2: parse error → HIGH invalid
        p = write(d, "bad.yaml", "a: [unclosed\n")
        doc, fs = safe_load_mapping(p, "x")
        ok(doc is None and fs and fs[0]["kind"] == "x_invalid"
           and fs[0]["severity"] == "HIGH", "S2 parse error → HIGH invalid")
        ok(fs and fs[0]["scope_declarable"] is False,
           "S2b invalid is not scope-declarable")

        # S3: top-level list → HIGH invalid
        p = write(d, "list.yaml", "- a\n- b\n")
        doc, fs = safe_load_mapping(p, "x")
        ok(doc is None and fs and fs[0]["kind"] == "x_invalid",
           "S3 top-level list → invalid")

        # S4: empty file (None) → invalid (not {})
        p = write(d, "empty.yaml", "")
        doc, fs = safe_load_mapping(p, "x")
        ok(doc is None and fs and fs[0]["kind"] == "x_invalid",
           "S4 empty file → invalid")

        # S5: missing required key (renamed key) → invalid
        p = write(d, "renamed.yaml", "high_concerns: []\n")
        doc, fs = safe_load_mapping(p, "pm", required_keys=["concerns"])
        ok(doc is None and fs and "concerns" in fs[0]["claim"],
           "S5 renamed key → invalid")

        # S6: wrong type → invalid
        p = write(d, "type.yaml", "violations: not-a-list\n")
        doc, fs = safe_load_mapping(p, "ca", expected_types={"violations": list})
        ok(doc is None and fs and fs[0]["kind"] == "ca_invalid",
           "S6 wrong type → invalid")

        # S7: unknown verdict → doc returned + HIGH unknown_verdict
        p = write(d, "v.yaml", "verdict: DEGENERATE_FIT_ACKNOWLEDGED\n")
        doc, fs = safe_load_mapping(
            p, "msc", allowed_verdicts=["CLEAN", "DEGENERATE_FIT_DETECTED"])
        ok(doc is not None and fs and fs[0]["kind"] == "msc_unknown_verdict",
           "S7 invented verdict → unknown_verdict HIGH")

        # S8: allowed verdict → no findings
        p = write(d, "v2.yaml", "verdict: CLEAN\n")
        doc, fs = safe_load_mapping(p, "msc", allowed_verdicts=["CLEAN"])
        ok(doc == {"verdict": "CLEAN"} and fs == [], "S8 known verdict is silent")

        # S9: missing verdict key with allowlist → invalid
        p = write(d, "v3.yaml", "other: 1\n")
        doc, fs = safe_load_mapping(p, "msc", allowed_verdicts=["CLEAN"])
        ok(doc is None and fs and fs[0]["kind"] == "msc_invalid",
           "S9 missing verdict key → invalid")

    # S10: run_guarded converts exceptions
    def boom(_x):
        raise AttributeError("'list' object has no attribute 'get'")
    fs = run_guarded(boom, 1)
    ok(len(fs) == 1 and fs[0]["kind"] == "validator_check_crashed"
       and fs[0]["severity"] == "HIGH" and "boom" in fs[0]["claim"],
       "S10 crash → validator_check_crashed HIGH")
    ok(run_guarded(lambda: None) == [], "S10b None → []")
    ok(run_guarded(lambda: [{"kind": "k"}]) == [{"kind": "k"}],
       "S10c passthrough")
    ok(run_guarded(lambda: {"a": 1})[0]["kind"] == "validator_check_crashed",
       "S10d non-list → crashed")

    # S11: finite_number
    ok(finite_number("3.5") == 3.5, "S11 numeric string")
    ok(finite_number(float("nan")) is None, "S11b NaN rejected")
    ok(finite_number(float("inf")) is None, "S11c inf rejected")
    ok(finite_number(True) is None, "S11d bool rejected")
    ok(finite_number(None) is None, "S11e None rejected")
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        os.makedirs(os.path.join(d, "run", "models"))
        os.symlink(out, os.path.join(d, "run", "linked"))
        run = os.path.join(d, "run")
        ok(inside_dir(run, os.path.join(run, "models", "x.csv")), "S12a plain path inside")
        ok(not inside_dir(run, os.path.join(run, "linked", "x.csv")), "S12b symlinked parent escapes")
        ok(not inside_dir(run, os.path.join(run, "..", "x.csv")), "S12c .. escapes")
        ok(not inside_dir(run, run + "2/x"), "S12d sibling prefix is not inside")

    # S12: sha256_file / read_jsonl (lenient readers)
    with tempfile.TemporaryDirectory() as d:
        p = write(d, "a.txt", "abc")
        ok(sha256_file(p) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
           "S12 sha256 of 'abc'")
        ok(sha256_file(os.path.join(d, "missing")) is None, "S12b missing → None")
        j = write(d, "a.jsonl", '{"a": 1}\nnot json\n[2]\n')
        ok(read_jsonl(j) == [{"a": 1}, [2]], "S12c bad line skipped")
        ok(read_jsonl(os.path.join(d, "missing")) == [], "S12d missing → []")

    if failures:
        print("FAIL:", file=sys.stderr)
        for fl in failures:
            print(f"  - {fl}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(0)
