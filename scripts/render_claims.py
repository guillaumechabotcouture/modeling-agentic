"""Phase 18 α — Render `[CLAIM:id]` references in report.md.

The writer drafts `report.md` using `[CLAIM:claim_id]` references for
every numeric / percentage / currency / count / verdict-label / citation
claim. This script reads `models/claims_ledger.yaml`, looks up each
referenced claim, formats it, and substitutes
the rendered string back into `report.md`. Free prose is preserved.

Why this exists
---------------
Phase 17 retro on 2026-04-29_161312 had multiple drift classes that
survived to ACCEPT (R-019 verdict mismatch, R-020 LGA count drift,
$197M cost conflation). The coherence auditor catches drift after
the fact; the claims ledger PREVENTS drift structurally — the writer
cannot re-narrate a number that doesn't exist in the ledger.

Phase 22 γ: when `models/claims_spec.yaml` exists, the ledger is first
rebuilt from it (scripts/build_claims_ledger.py), so every rendered
number is read from its source artifact at render time.

Usage
-----
    python3 scripts/render_claims.py {run_dir}
    python3 scripts/render_claims.py --self-test

Output
------
- `report.md` (rewritten in place with claims rendered)
- `report.unrendered.md` (snapshot of the writer's draft for debugging)

Exit codes
----------
- 0: all references rendered successfully
- 1: one or more references unresolved (typo'd claim ID, missing
     ledger). report.md is left in writer's-draft form; details
     printed to stderr.

Phase 21 γ: report.md is genuinely left untouched on unresolved
references (it previously received a partial render, which a re-run
then copied over report.unrendered.md, losing the writer's draft).
Claim ids may contain `.` and `-`; any `[CLAIM:...]`-shaped token that
does not resolve — malformed id included — is reported as unresolved.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from typing import Any

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.text_spans import code_spans, in_spans  # noqa: E402

# Phase 21 γ: ids may use `.` and `-` (e.g. `cases-averted`, `h2.verdict`);
# the old `[A-Za-z0-9_]+` pattern left such tokens raw AND uncounted.
_CLAIM_ID_CHARS = r"A-Za-z0-9_.\-"
_CLAIM_ID_RE = re.compile(rf"[{_CLAIM_ID_CHARS}]+")
# Any `[CLAIM:...]`-shaped token (single line). Tokens whose payload is
# not a well-formed id are unresolved by construction.
_CLAIM_REF_RE = re.compile(r"\[CLAIM:([^\]\n]*)\]")

# Kinds whose claims must carry a non-null `value`. Citation kinds are
# always required to have a value (the citation string); the other
# value-bearing kinds are listed explicitly so future kinds can opt out
# if there's a real reason. Phase 20 α: closes the null-value gap that
# rendered `[CLAIM:id:NO_VALUE]` markers into report.md unflagged.
_VALUE_REQUIRED_KINDS = frozenset({
    "scalar", "count", "label", "percentage", "currency", "citation",
})


# Phase 21 γ: shared with coherence_audit.py via scripts/lib/text_spans.py.
def _fenced_or_inline_code_spans(text: str) -> list[tuple[int, int]]:
    return code_spans(text, inline=True)


def _in_protected_span(spans: list[tuple[int, int]], pos: int) -> bool:
    return in_spans(spans, pos)

# ----- Magnitude helpers ------------------------------------------------ #


_SUFFIXES = ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K"))


def _strip_zeros(s: str) -> str:
    return s.rstrip("0").rstrip(".") if "." in s else s


def _scaled(v: float, decimals: int) -> str:
    """Scale `v` to a B/M/K suffix with `decimals` places.

    Phase 21 γ: the suffix is chosen AFTER rounding, so 999,600 renders
    as 1M rather than 1000K (and 999,999,999 as 1B, not 1000M).
    """
    av = abs(v)
    for i, (div, suf) in enumerate(_SUFFIXES):
        if av >= div:
            body = f"{v / div:.{decimals}f}"
            if abs(float(body)) >= 1000 and i > 0:
                up_div, up_suf = _SUFFIXES[i - 1]
                return _strip_zeros(f"{v / up_div:.{decimals}f}") + up_suf
            return _strip_zeros(body) + suf
    # Below 1,000: rounding can still carry into the K band.
    if round(av, decimals) >= 1000:
        return _strip_zeros(f"{v / 1000:.{decimals}f}") + "K"
    return ""


def _small(v: float, decimals: int) -> str:
    """Render |v| < 1000 without losing small magnitudes.

    Phase 21 γ: 0.0005 rendered as "0" under fixed decimals; values whose
    fixed-decimal rendering would drop all significant digits keep two
    significant figures instead.
    """
    if float(v).is_integer():
        return str(int(v))
    fixed = _strip_zeros(f"{v:.{decimals}f}")
    if fixed not in ("0", "-0"):
        return fixed
    import math
    digits = max(decimals, -int(math.floor(math.log10(abs(v)))) + 1)
    return _strip_zeros(f"{v:.{digits}f}")


def _format_magnitude(v: float, decimals: int = 2) -> str:
    """Render a number with M/K suffix appropriate to magnitude.

    >>> _format_magnitude(7467997)
    '7.47M'
    >>> _format_magnitude(320_000_000)
    '320M'
    >>> _format_magnitude(2_300_000_000)
    '2.3B'
    >>> _format_magnitude(63)
    '63'
    >>> _format_magnitude(0.0005)
    '0.0005'
    """
    return _scaled(v, decimals) or _small(v, decimals)


def _format_currency(v: float, decimals: int = 1) -> str:
    """Render a currency value with $ prefix and magnitude suffix.

    Phase 21 γ: sub-$1,000 non-integer amounts keep cents ($0.85, not $1);
    thousands keep one decimal ($1.2K, not $1K).
    """
    sign = "-" if v < 0 else ""
    av = abs(v)
    scaled = _scaled(av, decimals)
    if scaled:
        return f"{sign}${scaled}"
    if float(av).is_integer():
        return f"{sign}${int(av)}"
    return f"{sign}${av:.2f}"


def _format_interval(lo: str, hi: str, lo_num: float, hi_num: float) -> str:
    """Phase 21 γ: `lo-hi` is ambiguous when a bound is negative
    ("-5-3"); use "lo to hi" then."""
    if lo_num < 0 or hi_num < 0:
        return f"{lo} to {hi}"
    return f"{lo}-{hi}"


# ----- Per-kind renderers ----------------------------------------------- #


def render_claim(claim: dict) -> str:
    """Render a single claim entry to its display string.

    Format depends on `claim_kind`. Includes a 95%-CI suffix for
    scalar/percentage claims that have ci_low + ci_high populated.
    """
    kind = (claim.get("claim_kind") or "").lower()
    val = claim.get("value")
    if val is None:
        return f"[CLAIM:{claim.get('id', '?')}:NO_VALUE]"

    ci_low = claim.get("ci_low")
    ci_high = claim.get("ci_high")
    has_ci = ci_low is not None and ci_high is not None

    # v2 (spike lesson 6): an interval says what it covers. The analysis engine
    # writes `interval_label` (e.g. "95% UI over engine parameters and
    # calibration"); it replaces the generic "95% CI".
    label = claim.get("interval_label")
    label = label.strip() if isinstance(label, str) and label.strip() else "95% CI"

    def _ci(fmt) -> str:
        lo, hi = float(ci_low), float(ci_high)
        return f" ({label}: {_format_interval(fmt(lo), fmt(hi), lo, hi)})"

    def _pct(x: float) -> str:
        return f"{x:.1f}%".replace(".0%", "%")

    if kind == "scalar":
        body = _format_magnitude(float(val))
        if has_ci:
            body += _ci(_format_magnitude)
        return body

    if kind == "count":
        # Phase 21 γ: round, don't truncate (12.7 → 13).
        return str(int(round(float(val))))

    if kind == "percentage":
        body = _pct(float(val))
        if has_ci:
            body += _ci(_pct)
        return body

    if kind == "currency":
        body = _format_currency(float(val))
        if has_ci:
            body += _ci(_format_currency)
        return body

    if kind == "label":
        # Verdict labels render uppercase verbatim
        return str(val).upper()

    if kind == "citation":
        return str(val)

    # Unknown kind: return the value as-is, lossy
    return str(val)


# ----- Ledger loading + validation -------------------------------------- #


_VALID_KINDS = frozenset({
    "scalar", "count", "label", "percentage", "currency", "citation",
})
_NUMERIC_KINDS = frozenset({"scalar", "count", "percentage", "currency"})


def load_claims(ledger_path: str) -> dict[str, dict]:
    """Read the ledger and return {claim_id: claim_dict}.

    Validates internal consistency: ci_low <= value <= ci_high for
    scalar/percentage claims that declare CIs. Raises ValueError on
    schema violations.
    """
    if not os.path.exists(ledger_path):
        raise FileNotFoundError(f"claims_ledger not found: {ledger_path}")
    with open(ledger_path) as f:
        doc = yaml.safe_load(f) or {}
    raw = doc.get("claims") or []
    if not isinstance(raw, list):
        raise ValueError(f"{ledger_path}: top-level `claims:` must be a list")
    out: dict[str, dict] = {}
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            raise ValueError(f"{ledger_path}: claims[{i}] is not a mapping")
        cid = c.get("id")
        if not isinstance(cid, str) or not cid:
            raise ValueError(f"{ledger_path}: claims[{i}] missing string `id`")
        # Phase 21 γ: an id the reference syntax cannot express can never
        # be rendered; reject it here instead of leaving raw tokens.
        if not _CLAIM_ID_RE.fullmatch(cid):
            raise ValueError(
                f"{ledger_path}: claims[{i}] id {cid!r} must match "
                f"[{_CLAIM_ID_CHARS}]+")
        if cid in out:
            raise ValueError(f"{ledger_path}: duplicate claim id {cid!r}")
        kind = c.get("claim_kind")
        if kind not in _VALID_KINDS:
            raise ValueError(
                f"{ledger_path}: claims[{i}] kind {kind!r} not in "
                f"{sorted(_VALID_KINDS)}")
        # Phase 20 α: every value-bearing claim must have a non-null
        # `value`. Without this check, render_claim() emitted a
        # `[CLAIM:id:NO_VALUE]` marker that escaped the audit's raw-claim
        # regex (which only matched bare `[CLAIM:id]` tokens) and
        # shipped to report.md.
        if kind in _VALUE_REQUIRED_KINDS and c.get("value") is None:
            raise ValueError(
                f"{ledger_path}: claims[{i}] {cid!r} of kind {kind!r} "
                f"must have a non-null `value` field")
        v = c.get("value")
        # Phase 21 γ: YAML coerces `yes`/`no`/`true` to booleans; a label
        # claim must be the verdict string itself, and numeric kinds must
        # be numbers (render_claim would otherwise crash or print "TRUE").
        if isinstance(v, bool):
            raise ValueError(
                f"{ledger_path}: claims[{i}] {cid!r} value is a YAML "
                f"boolean ({v!r}); quote it if it is a label")
        if kind in _NUMERIC_KINDS and v is not None:
            try:
                fv_check = float(v)
            except (TypeError, ValueError):
                raise ValueError(
                    f"{ledger_path}: claims[{i}] {cid!r} of kind {kind!r} "
                    f"has non-numeric value {v!r}")
            if fv_check != fv_check or fv_check in (float("inf"),
                                                    float("-inf")):
                raise ValueError(
                    f"{ledger_path}: claims[{i}] {cid!r} value is not finite")
        # CI consistency (Phase 21 γ: currency and count too)
        if kind in _NUMERIC_KINDS:
            lo = c.get("ci_low")
            hi = c.get("ci_high")
            if (lo is None) != (hi is None):
                raise ValueError(
                    f"{ledger_path}: claims[{i}] {cid!r} declares only one "
                    f"CI bound")
            if lo is not None and hi is not None and v is not None:
                try:
                    fv, flo, fhi = float(v), float(lo), float(hi)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"{ledger_path}: claims[{i}] CI fields must be numeric")
                if not (flo <= fv <= fhi):
                    raise ValueError(
                        f"{ledger_path}: claims[{i}] {cid!r} CI bounds "
                        f"violated: {flo} <= {fv} <= {fhi}")
        out[cid] = c
    return out


# ----- Render driver ----------------------------------------------------- #


def render_text(text: str, claims: dict[str, dict]) -> tuple[str, list[str]]:
    """Substitute every `[CLAIM:id]` reference in `text` with its rendered
    value. Returns (new_text, unresolved_ids). If unresolved IDs exist,
    the caller decides whether to fail or write a partial render.

    Phase 20 α: `[CLAIM:id]` tokens inside fenced code blocks or inline
    `code` spans are left intact so the writer can document the syntax
    in report.md without those examples getting substituted.
    """
    unresolved: list[str] = []
    protected = _fenced_or_inline_code_spans(text)

    def _sub(m: re.Match) -> str:
        if _in_protected_span(protected, m.start()):
            return m.group(0)
        cid = m.group(1).strip()
        c = claims.get(cid) if _CLAIM_ID_RE.fullmatch(cid or "") else None
        if c is None:
            unresolved.append(cid or "<empty>")
            return m.group(0)  # leave reference intact
        return render_claim(c)

    new_text = _CLAIM_REF_RE.sub(_sub, text)
    return new_text, unresolved


def _builder():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "build_claims_ledger", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "build_claims_ledger.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _claim_units():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_ma_claim_units", os.path.join(os.path.dirname(os.path.abspath(__file__)), "claim_units.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def render_run(run_dir: str) -> dict:
    """Render report.md in-place using the run's claims ledger.

    Side effects:
    - Snapshot the writer's draft to `report.unrendered.md` (only if
      a `[CLAIM:` reference is found — otherwise no-op).
    - Rewrite `report.md` with claims rendered.

    Returns a result dict {ok, n_refs, n_unresolved, unresolved_ids,
    snapshot_path, report_path}.
    """
    report_path = os.path.join(run_dir, "report.md")
    # Phase 25: the writer drafts manuscript.md (the CLI blocks subagents
    # from writing REPORT*.md); this script writes report.md from it.
    draft_path = os.path.join(run_dir, "manuscript.md")
    source_path = draft_path if os.path.exists(draft_path) else report_path
    ledger_path = os.path.join(run_dir, "models", "claims_ledger.yaml")
    if not os.path.exists(source_path):
        return {"ok": False, "error": f"neither manuscript.md nor report.md found in {run_dir}",
                "n_refs": 0, "n_unresolved": 0, "unresolved_ids": []}
    if not os.path.exists(ledger_path) and not os.path.exists(
            os.path.join(run_dir, "models", "claims_spec.yaml")):
        return {"ok": False,
                "error": f"claims_ledger.yaml not found at {ledger_path}",
                "n_refs": 0, "n_unresolved": 0, "unresolved_ids": []}

    with open(source_path) as f:
        text = f.read()

    def _write_report(body):
        with open(report_path, "w") as f:
            f.write(body)

    protected = _fenced_or_inline_code_spans(text)
    n_refs = sum(1 for m in _CLAIM_REF_RE.finditer(text)
                 if not _in_protected_span(protected, m.start()))
    if n_refs == 0:
        # No references: the draft is the report
        if source_path != report_path:
            _write_report(text)
        return {"ok": True, "n_refs": 0, "n_unresolved": 0,
                "unresolved_ids": [], "snapshot_path": None,
                "report_path": report_path}

    if os.path.exists(os.path.join(run_dir, "models", "claims_spec.yaml")):
        _, spec_errors = _builder().write(run_dir)
        if spec_errors:
            return {"ok": False, "error": "claims_spec.yaml: " + "; ".join(spec_errors[:5]),
                    "n_refs": n_refs, "n_unresolved": 0, "unresolved_ids": []}
    claims = load_claims(ledger_path)

    # Snapshot the writer's draft before substitution.
    snapshot_path = os.path.join(run_dir, "report.unrendered.md")
    shutil.copy2(source_path, snapshot_path)

    new_text, unresolved = render_text(text, claims)

    if unresolved:
        # Phase 21 γ: leave report.md as the writer's draft (as the module
        # docstring always promised). Writing the partial render here
        # meant a second invocation snapshotted half-rendered text over
        # report.unrendered.md, destroying the draft.
        if source_path != report_path:
            _write_report(text)        # report.md = the unrendered draft, as before
        return {"ok": False, "n_refs": n_refs,
                "n_unresolved": len(set(unresolved)),
                "unresolved_ids": sorted(set(unresolved)),
                "snapshot_path": snapshot_path,
                "report_path": report_path}

    _write_report(new_text)
    # Phase 47 β (A18): percentage claims cited here whose source holds a
    # proportion (rendered 100 times too small). Reported, not blocking: the
    # gate raises them (claim_percentage_is_proportion).
    try:
        cited = {m.group(1).strip() for m in _CLAIM_REF_RE.finditer(text)}
        scale = [p for p in _claim_units().problems(list(claims.values()), run_dir)
                 if p["id"] in cited]
    except Exception as e:  # noqa: BLE001 — a failed check must be visible, not silent
        scale = [{"id": "?", "rendered": "?", "as_percent": "?",
                  "evidence": [f"check failed: {type(e).__name__}: {e}"]}]
    return {"ok": True, "n_refs": n_refs, "n_unresolved": 0,
            "unresolved_ids": [], "snapshot_path": snapshot_path,
            "report_path": report_path, "scale_problems": scale}


# ----- Self-test --------------------------------------------------------- #


def _self_test() -> int:
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    # T1: render_claim formats each kind correctly
    s = render_claim({"id": "x", "claim_kind": "scalar", "value": 7467997,
                      "ci_low": 5140000, "ci_high": 10420000})
    ok("7.47M" in s and "5.14M" in s and "10.42M" in s,
       f"T1 scalar+CI: got {s!r}")
    ok("95% CI:" in s, f"T1 the default interval label is 95% CI: {s!r}")
    s = render_claim({"id": "x", "claim_kind": "scalar", "value": 7467997, "ci_low": 5140000,
                      "ci_high": 10420000, "interval_label": "95% UI over engine parameters"})
    ok("(95% UI over engine parameters: 5.14M" in s and "CI" not in s,
       f"T1b v2 lesson 6: an interval_label replaces the generic label: {s!r}")
    s = render_claim({"id": "x", "claim_kind": "scalar", "value": 320_000_000})
    ok(s == "320M", f"T1 scalar no-CI: got {s!r}")
    s = render_claim({"id": "x", "claim_kind": "count", "value": 63})
    ok(s == "63", f"T1 count: got {s!r}")
    s = render_claim({"id": "x", "claim_kind": "percentage", "value": 52.5})
    ok(s == "52.5%", f"T1 percentage: got {s!r}")
    s = render_claim({"id": "x", "claim_kind": "currency",
                      "value": 320_000_000})
    ok(s == "$320M", f"T1 currency: got {s!r}")
    s = render_claim({"id": "x", "claim_kind": "label", "value": "robust"})
    ok(s == "ROBUST", f"T1 label uppercase: got {s!r}")

    # T2: render_text substitutes references; preserves prose
    claims = {
        "dalys": {"id": "dalys", "claim_kind": "scalar",
                  "value": 7467997, "ci_low": 5140000, "ci_high": 10420000},
        "verdict": {"id": "verdict", "claim_kind": "label", "value": "ROBUST"},
        "share": {"id": "share", "claim_kind": "percentage", "value": 52.5},
    }
    text = ("The allocation averts [CLAIM:dalys] DALYs/yr; "
            "sensitivity verdict is [CLAIM:verdict]. North West receives "
            "[CLAIM:share] of the budget.")
    new_text, un = render_text(text, claims)
    ok(not un, f"T2 unresolved: {un}")
    ok("7.47M" in new_text and "ROBUST" in new_text and "52.5%" in new_text,
       f"T2 substitutions: {new_text!r}")
    ok("[CLAIM:" not in new_text,
       f"T2 should leave no unresolved references in fully-bound text: "
       f"{new_text!r}")

    # T3: unresolved reference is preserved + reported
    text = "The model averts [CLAIM:typo_id] DALYs."
    new_text, un = render_text(text, claims)
    ok(un == ["typo_id"], f"T3 unresolved list: {un}")
    ok("[CLAIM:typo_id]" in new_text,
       f"T3 unresolved kept inline: {new_text!r}")

    # T4: load_claims accepts well-formed ledger
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        ledger_path = os.path.join(d, "models", "claims_ledger.yaml")
        with open(ledger_path, "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "a", "claim_kind": "scalar", "value": 100,
                 "ci_low": 80, "ci_high": 120},
                {"id": "b", "claim_kind": "label", "value": "ROBUST"},
            ]}, f)
        loaded = load_claims(ledger_path)
        ok("a" in loaded and "b" in loaded,
           f"T4 load: ids missing from {loaded.keys()}")

    # T5: load_claims rejects CI bounds violation
    with tempfile.TemporaryDirectory() as d:
        ledger_path = os.path.join(d, "claims_ledger.yaml")
        with open(ledger_path, "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "bad", "claim_kind": "scalar", "value": 50,
                 "ci_low": 80, "ci_high": 120},
            ]}, f)
        try:
            load_claims(ledger_path)
            ok(False, "T5: should have raised on ci bounds violation")
        except ValueError as e:
            ok("CI bounds violated" in str(e), f"T5 error msg: {e}")

    # T6: load_claims rejects duplicate ids
    with tempfile.TemporaryDirectory() as d:
        ledger_path = os.path.join(d, "claims_ledger.yaml")
        with open(ledger_path, "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "a", "claim_kind": "scalar", "value": 1},
                {"id": "a", "claim_kind": "scalar", "value": 2},
            ]}, f)
        try:
            load_claims(ledger_path)
            ok(False, "T6: should have raised on duplicate id")
        except ValueError as e:
            ok("duplicate" in str(e), f"T6 error msg: {e}")

    # T7: render_run end-to-end on a synthetic run dir
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "claims_ledger.yaml"), "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "dalys", "claim_kind": "scalar",
                 "value": 7467997, "ci_low": 5140000, "ci_high": 10420000},
                {"id": "verdict", "claim_kind": "label", "value": "ROBUST"},
            ]}, f)
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("# Report\n\nThe allocation averts [CLAIM:dalys] DALYs.\n"
                    "Sensitivity verdict: [CLAIM:verdict].\n")
        result = render_run(d)
        ok(result["ok"] is True,
           f"T7 ok: {result}")
        ok(result["n_refs"] == 2, f"T7 n_refs: {result}")
        with open(os.path.join(d, "report.md")) as f:
            rendered = f.read()
        ok("7.47M" in rendered and "ROBUST" in rendered,
           f"T7 rendered text: {rendered!r}")
        # snapshot present
        ok(os.path.exists(os.path.join(d, "report.unrendered.md")),
           "T7: report.unrendered.md snapshot must exist after render")

    # T7b (Phase 25): the writer's draft is manuscript.md; report.md is built from it
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "claims_ledger.yaml"), "w") as f:
            yaml.safe_dump({"claims": [{"id": "dalys", "claim_kind": "scalar", "value": 7467997}]}, f)
        with open(os.path.join(d, "manuscript.md"), "w") as f:
            f.write("# Report\n\nAverts [CLAIM:dalys] DALYs.\n")
        result = render_run(d)
        rendered = open(os.path.join(d, "report.md")).read() if os.path.exists(os.path.join(d, "report.md")) else ""
        ok(result["ok"] and "7.47M" in rendered and "[CLAIM:" not in rendered
           and "[CLAIM:dalys]" in open(os.path.join(d, "report.unrendered.md")).read(),
           f"T7b report.md rendered from manuscript.md: {result}")
        os.remove(os.path.join(d, "report.md"))
        with open(os.path.join(d, "manuscript.md"), "w") as f:
            f.write("# Report\n\nNo claim references here.\n")
        render_run(d)
        ok(os.path.exists(os.path.join(d, "report.md")), "T7b a draft without references still becomes report.md")

    # T8: render_run reports unresolved references gracefully
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "claims_ledger.yaml"), "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "a", "claim_kind": "scalar", "value": 100},
            ]}, f)
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("Got [CLAIM:a] and [CLAIM:b_typo] both.\n")
        result = render_run(d)
        ok(not result["ok"], f"T8 should fail with unresolved: {result}")
        ok(result["unresolved_ids"] == ["b_typo"],
           f"T8 unresolved list: {result}")

    # T9 (Phase 20 α): load_claims rejects null `value` for value-bearing
    # kinds. Closes the gap where `[CLAIM:id:NO_VALUE]` markers escaped
    # the audit's raw-claim regex.
    with tempfile.TemporaryDirectory() as d:
        ledger_path = os.path.join(d, "claims_ledger.yaml")
        with open(ledger_path, "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "novalue", "claim_kind": "scalar", "value": None},
            ]}, f)
        try:
            load_claims(ledger_path)
            ok(False, "T9: should have raised on null value for scalar")
        except ValueError as e:
            ok("non-null" in str(e),
               f"T9 error msg: {e}")

    # T10 (Phase 20 α): `[CLAIM:id]` inside a fenced code block is left
    # intact so the writer can document the syntax in report.md.
    claims_t10 = {
        "x": {"id": "x", "claim_kind": "scalar", "value": 100},
    }
    text = ("Use [CLAIM:x] in prose, but inside\n"
            "```\n"
            "Example: [CLAIM:x]\n"
            "```\n"
            "the reference is preserved.\n")
    new_text, un = render_text(text, claims_t10)
    ok(not un, f"T10 unresolved: {un}")
    ok("Example: [CLAIM:x]" in new_text,
       f"T10 fenced reference must be preserved: {new_text!r}")
    ok("Use 100 in prose" in new_text,
       f"T10 inline reference must be rendered: {new_text!r}")

    # T11 (Phase 20 α): `[CLAIM:id]` inside inline backticks is left
    # intact.
    text = "The token `[CLAIM:x]` is documented syntax; [CLAIM:x] is a real claim."
    new_text, un = render_text(text, claims_t10)
    ok("`[CLAIM:x]`" in new_text,
       f"T11 inline-code reference must be preserved: {new_text!r}")
    ok(new_text.count("100") == 1,
       f"T11 only one substitution should occur: {new_text!r}")

    # ---- Phase 21 γ ---------------------------------------------------- #

    # T12: hyphen / dot ids render; malformed or empty tokens are
    # unresolved rather than silently left raw.
    claims_t12 = {
        "cases-averted": {"id": "cases-averted", "claim_kind": "count",
                          "value": 4120},
        "h2.verdict": {"id": "h2.verdict", "claim_kind": "label",
                       "value": "supported"},
    }
    new_text, un = render_text(
        "Averts [CLAIM:cases-averted] cases; H2 is [CLAIM:h2.verdict]. "
        "Bad: [CLAIM:has space] and [CLAIM:].", claims_t12)
    ok("Averts 4120 cases" in new_text and "H2 is SUPPORTED" in new_text,
       f"T12 hyphen/dot ids must render: {new_text!r}")
    ok(sorted(un) == ["<empty>", "has space"],
       f"T12 malformed tokens must be unresolved: {un}")

    # T13: precision-preserving formatting.
    cases_t13 = [
        ({"claim_kind": "scalar", "value": 0.0005}, "0.0005"),
        ({"claim_kind": "scalar", "value": 999_999}, "1M"),
        ({"claim_kind": "currency", "value": 999_600}, "$999.6K"),
        ({"claim_kind": "currency", "value": 999_960}, "$1M"),
        ({"claim_kind": "currency", "value": 999_999_999}, "$1B"),
        ({"claim_kind": "currency", "value": 0.85}, "$0.85"),
        ({"claim_kind": "currency", "value": 1_234}, "$1.2K"),
        ({"claim_kind": "currency", "value": 12.8e6}, "$12.8M"),
        ({"claim_kind": "currency", "value": -2.5e6}, "-$2.5M"),
        ({"claim_kind": "count", "value": 12.7}, "13"),
    ]
    for c, want in cases_t13:
        got = render_claim({"id": "x", **c})
        ok(got == want, f"T13 {c} → {got!r}, want {want!r}")

    # T14: negative CI uses "to" separator, not "-5-3".
    got = render_claim({"id": "x", "claim_kind": "scalar", "value": -1,
                        "ci_low": -5, "ci_high": 3})
    ok(got == "-1 (95% CI: -5 to 3)", f"T14 negative CI: {got!r}")

    # T15: ledger validation — currency CI bounds, booleans, non-numeric,
    # one-sided CI, ids the syntax cannot express.
    bad_ledgers = {
        "currency_ci": {"id": "c", "claim_kind": "currency", "value": 5e6,
                        "ci_low": 9e6, "ci_high": 1e6},
        "bool_label": {"id": "l", "claim_kind": "label", "value": True},
        "str_scalar": {"id": "s", "claim_kind": "scalar", "value": "~0.3"},
        "one_sided": {"id": "o", "claim_kind": "scalar", "value": 1,
                      "ci_low": 0},
        "bad_id": {"id": "has space", "claim_kind": "scalar", "value": 1},
    }
    for label, entry in bad_ledgers.items():
        with tempfile.TemporaryDirectory() as d:
            lp = os.path.join(d, "claims_ledger.yaml")
            with open(lp, "w") as f:
                yaml.safe_dump({"claims": [entry]}, f)
            try:
                load_claims(lp)
                ok(False, f"T15 {label}: should have raised")
            except ValueError:
                pass

    # T16: unresolved references leave report.md untouched, and a re-run
    # does not overwrite the snapshot with half-rendered text.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "claims_ledger.yaml"), "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "a", "claim_kind": "scalar", "value": 100}]}, f)
        draft = "Got [CLAIM:a] and [CLAIM:b_typo].\n"
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write(draft)
        render_run(d)
        render_run(d)
        with open(os.path.join(d, "report.md")) as f:
            ok(f.read() == draft, "T16: report.md must stay the draft")
        with open(os.path.join(d, "report.unrendered.md")) as f:
            ok(f.read() == draft, "T16: snapshot must stay the draft")

    # T17 (Phase 22 γ): with a claims spec, the rendered number comes from
    # the source artifact, even when a stale ledger file says otherwise.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "out.yaml"), "w") as f:
            yaml.safe_dump({"n_units": 208}, f)
        with open(os.path.join(d, "models", "claims_spec.yaml"), "w") as f:
            yaml.safe_dump({"claims": [{"id": "n", "claim_kind": "count",
                                        "source_artifact": "models/out.yaml",
                                        "source_field": "n_units"}]}, f)
        with open(os.path.join(d, "models", "claims_ledger.yaml"), "w") as f:
            yaml.safe_dump({"claims": [{"id": "n", "claim_kind": "count", "value": 150}]}, f)
        with open(os.path.join(d, "report.md"), "w") as f:
            f.write("We fund [CLAIM:n] units.\n")
        r = render_run(d)
        with open(os.path.join(d, "report.md")) as f:
            body = f.read()
        ok(r["ok"] and "208" in body and "150" not in body,
           f"T17: spec-built ledger wins over a stale file ({r}, {body!r})")

    # T18 (Phase 47 β, A18): a cited percentage claim whose source holds a
    # proportion is reported with the render (attempt 7, R-064: 0.587 -> "0.6%").
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "out.yaml"), "w") as f:
            yaml.safe_dump({"a": {"gain_pct": 10.4, "p10": 0.587, "small_pct": 0.5}}, f)
        with open(os.path.join(d, "models", "claims_spec.yaml"), "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "p", "claim_kind": "percentage", "source_artifact": "models/out.yaml",
                 "source_field": "a.p10"},
                {"id": "s", "claim_kind": "percentage", "source_artifact": "models/out.yaml",
                 "source_field": "a.small_pct"}]}, f)
        with open(os.path.join(d, "manuscript.md"), "w") as f:
            f.write("P = [CLAIM:p]; change [CLAIM:s].\n")
        r = render_run(d)
        ok(r["ok"] and [p["id"] for p in r.get("scale_problems", [])] == ["p"]
           and r["scale_problems"][0]["rendered"] == "0.6%",
           f"T18: a proportion cited as a percentage is reported; a real 0.5% is not: {r}")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", nargs="?")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()
    if args.self_test:
        return _self_test()
    if not args.run_dir:
        p.error("run_dir is required (or use --self-test)")

    try:
        result = render_run(args.run_dir)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not result["ok"] and "error" in result:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        return 1

    print(f"refs: {result['n_refs']}, unresolved: {result['n_unresolved']}",
          file=sys.stderr)
    for p in result.get("scale_problems") or []:
        print(f"WARNING [{p['id']}]: percentage claim rendered as {p['rendered']}, but its source "
              f"holds a proportion ({p['as_percent']} meant): {'; '.join(p['evidence'])}",
              file=sys.stderr)
    if result.get("unresolved_ids"):
        print(f"unresolved IDs:", file=sys.stderr)
        for cid in result["unresolved_ids"]:
            print(f"  - {cid}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
