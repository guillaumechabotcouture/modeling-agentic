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
import csv
import html
import math
import os
import re
import shutil
import sys
from typing import Any, Optional

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import provenance as P  # noqa: E402
from lib import scope_declarations as sd  # noqa: E402

_TS_FENCE_RE = re.compile(r"^(?:```|~~~)", re.MULTILINE)
_TS_INLINE_RE = re.compile(r"`[^`\n]+`")


def code_spans(text: str, inline: bool = True) -> list[tuple[int, int]]:
    """Return spans of fenced code blocks (``` / ~~~) and, when `inline` is True, inline backtick code."""
    spans: list[tuple[int, int]] = []
    starts = [m.start() for m in _TS_FENCE_RE.finditer(text)]
    for i in range(0, len(starts) - 1, 2):
        end_idx = text.find("\n", starts[i + 1])
        if end_idx == -1:
            end_idx = len(text)
        spans.append((starts[i], end_idx + 1))
    if inline:
        for m in _TS_INLINE_RE.finditer(text):
            if not any(s <= m.start() < e for s, e in spans):
                spans.append((m.start(), m.end()))
    return spans


def in_spans(spans: list[tuple[int, int]], pos: int) -> bool:
    return any(s <= pos < e for s, e in spans)

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


_SOURCE_ID_RE = re.compile(r"[a-z][a-z_]{0,40}")
# engine parameter names as the engine contract allows them (e.g. `efficacy:vax`, `lrr:a+b`): no spaces or free text
_PARAM_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_:+.\-]{0,63}")
INTERVAL_KEYS = {"level", "covers", "excludes", "prior_dominated", "spec_fixed", "fixed", "fixed_other"}


def interval_meta_problem(meta) -> str | None:
    """Why an `interval:` block is unusable (None when it is fine): a level in [0.5, 1), and `covers` (non-empty) and
    `excludes` as lists of source identifiers (lower-case letters and underscores, no free text). Optional (v2 priors):
    `prior_dominated`, `spec_fixed` (held at a spec-declared value) and `fixed`, lists of parameter identifiers, and
    `fixed_other`, a count."""
    if not isinstance(meta, dict) or set(meta) - INTERVAL_KEYS:
        return ("interval must be a mapping of level, covers, excludes, prior_dominated, spec_fixed, fixed and "
                "fixed_other only")
    for k in ("prior_dominated", "spec_fixed", "fixed"):
        v = meta.get(k, [])
        if not isinstance(v, list) or not all(isinstance(x, str) and _PARAM_ID_RE.fullmatch(x) for x in v):
            return f"interval.{k} must be a list of parameter identifiers (a letter, then [A-Za-z0-9_:+.-])"
    fo = meta.get("fixed_other", 0)
    if isinstance(fo, bool) or not isinstance(fo, int) or fo < 0:
        return "interval.fixed_other must be a count"
    lvl = meta.get("level")
    if isinstance(lvl, bool) or not isinstance(lvl, (int, float)) or not 0.5 <= float(lvl) < 1:
        return f"interval.level {lvl!r} must be a number in [0.5, 1)"
    for k in ("covers", "excludes"):
        v = meta.get(k, [] if k == "excludes" else None)
        if not isinstance(v, list) or (k == "covers" and not v) or \
                not all(isinstance(x, str) and _SOURCE_ID_RE.fullmatch(x) for x in v):
            return f"interval.{k} must be a list of source identifiers ([a-z_]), {'non-empty' if k == 'covers' else ''}"
    return None


def interval_label(meta: dict) -> str:
    """'95% UI over engine parameters, calibration; not covered: costs' from {level, covers, excludes}."""
    bad = interval_meta_problem(meta)
    if bad:
        raise ValueError(bad)
    words = lambda xs: ", ".join(x.replace("_", " ") for x in xs)  # noqa: E731
    lab = f"{float(meta['level']) * 100:g}% UI over {words(meta['covers'])}"
    if meta.get("prior_dominated"):
        lab += f"; set by declared ranges, not by data: {', '.join(meta['prior_dominated'])}"
    if meta.get("spec_fixed"):
        lab += f"; held fixed at the spec value: {', '.join(meta['spec_fixed'])}"
    fx, fo = list(meta.get("fixed") or []), int(meta.get("fixed_other") or 0)
    if fx or fo:
        other = f"{fo} {'other ' if fx else ''}engine parameter{'s' if fo != 1 else ''}"
        lab += "; held fixed: " + (", ".join(fx) + (f" and {other}" if fo else "") if fx else other)
    if meta.get("excludes"):
        lab += f"; not covered: {words(meta['excludes'])}"
    return lab


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

    # v2 (spike lesson 6, Codex P1-5): an interval says what it covers. The label is
    # derived from structured metadata (`interval: {level, covers, excludes}`),
    # never from free text; without it the default "95% CI" is unchanged.
    label = interval_label(claim["interval"]) if claim.get("interval") is not None else "95% CI"

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
_FORBIDDEN_ICER_STATUSES = frozenset({
    "dominated", "extendedly_dominated", "dominant", "no_health_difference",
    "less_health_lower_cost", "identical", "reference",
})


def _icer_status_problem(cid: str, claims_map: dict[str, dict]) -> str | None:
    """CE3 guard: an `.icer` claim is refused when its sibling `.status` claim is a non-ICER state."""
    if not cid.endswith(".icer"):
        return None
    sib = claims_map.get(cid[:-5] + ".status")
    if isinstance(sib, dict):
        st = str(sib.get("value") or "").strip()
        if st in _FORBIDDEN_ICER_STATUSES:
            return f"ICER claim {cid!r} is forbidden when sibling status is {st!r}"
    return None


def load_claims(ledger_path: str) -> dict[str, dict]:
    """Read the ledger and return {claim_id: claim_dict}.

    Validates internal consistency: ci_low <= value <= ci_high for
    scalar/percentage claims that declare CIs, and refuses planted `.icer`
    claims for dominated/degenerate strategies (CE3). Raises ValueError on
    schema violations.
    """
    out = _validate_claims(_read_claims(ledger_path), ledger_path)
    for cid in list(out):
        why = _icer_status_problem(cid, out)
        if why:
            raise ValueError(f"{ledger_path}: {why}")
    return out


def _read_claims(ledger_path: str) -> list:
    if not os.path.exists(ledger_path):
        raise FileNotFoundError(f"claims_ledger not found: {ledger_path}")
    with open(ledger_path) as f:
        doc = yaml.safe_load(f) or {}
    raw = doc.get("claims") or []
    if not isinstance(raw, list):
        raise ValueError(f"{ledger_path}: top-level `claims:` must be a list")
    return raw


def load_claims_tolerant(ledger_path: str) -> tuple[dict[str, dict], dict[str, str]]:
    """({id: claim} for the claims load_claims accepts, {id, or claims[i] without one: why} for the others).
    For a caller that reports each invalid claim itself (the v2 report, agents/stage_machine.py): one bad
    claim then costs that claim, not every reference in the report. A ledger that is not a list of claims is
    refused whole, as by load_claims; an id given twice is invalid both times."""
    good: dict[str, dict] = {}
    bad: dict[str, str] = {}
    try:
        raw = _read_claims(ledger_path)
    except AttributeError as e:          # a ledger that is not a mapping (load_claims raises this as it always did)
        raise ValueError(f"{ledger_path}: the ledger is not a mapping with a `claims:` list") from e
    for i, c in enumerate(raw):
        cid = c.get("id") if isinstance(c, dict) else None
        # a row without an id the reference syntax can express is named by position (no id can be `claims[i]`)
        name = cid if isinstance(cid, str) and _CLAIM_ID_RE.fullmatch(cid) else f"claims[{i}]"
        try:
            one = _validate_claims([c], ledger_path, i)
            if name in good or name in bad:
                raise ValueError(f"{ledger_path}: claims[{i}] duplicate claim id {name!r}")
        except Exception as e:  # noqa: BLE001 -- whatever one claim's checks raise (a wrong-typed field) is that claim's
            bad[name] = str(e) if isinstance(e, ValueError) else f"{ledger_path}: claims[{i}] {type(e).__name__}: {e}"
            good.pop(name, None)
            continue
        good.update(one)
    for cid in list(good):
        why = _icer_status_problem(cid, good)
        if why:
            bad[cid] = f"{ledger_path}: {why}"
            good.pop(cid, None)
    return good, bad


def _validate_claims(raw: list, ledger_path: str, start: int = 0) -> dict[str, dict]:
    """load_claims' checks on a list of claims, the first of which is claims[start]."""
    out: dict[str, dict] = {}
    for i, c in enumerate(raw, start):
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
        # v2 (Codex P1-5): interval labels come from structured metadata only.
        if "interval_label" in c:
            raise ValueError(f"{ledger_path}: claims[{i}] {cid!r}: free-text interval_label is not accepted; "
                             "give interval: {level, covers, excludes}")
        if c.get("interval") is not None:
            why = interval_meta_problem(c["interval"])
            if why:
                raise ValueError(f"{ledger_path}: claims[{i}] {cid!r}: {why}")
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
        if cid.endswith(".icer"):
            if kind not in _NUMERIC_KINDS or float(v) <= 0.0:
                raise ValueError(
                    f"{ledger_path}: claims[{i}] {cid!r} ICER claim must be a strictly positive finite number")
            if c.get("ci_low") is not None or c.get("ci_high") is not None or c.get("interval") is not None:
                raise ValueError(
                    f"{ledger_path}: claims[{i}] {cid!r} ICER claims must not carry intervals (use CE plane/CEAC)")
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


# One or two backticks, closed by the same run (CommonMark), on one line.
_INLINE_CODE_RE = re.compile(r"(?<!`)(`{1,2})(?!`)([^\n]+?)(?<!`)\1(?!`)")
# A fence line, also inside block quotes or list items ('>', blanks and list markers before it).
_FENCE_LINE_RE = re.compile(r"^(?:[ \t>]|[-*+][ \t]|\d{1,9}[.)][ \t])*(`{3,}|~{3,})")
_QUOTED_REF_RE = re.compile(r"\[CLAIM:([^\]\[\n]*)\]")    # one line, no bracket inside: never a link's bracket


def _code_block_lines(text: str) -> list[tuple[int, int]]:
    """Spans of fenced blocks (a fence after any indentation or block-quote markers, closed only by a fence of the
    same character at least as long) and of lines indented four spaces or a tab (an indented code block, or a
    nested list line: not normalised either way)."""
    spans, pos, fence = [], 0, None
    for line in text.splitlines(keepends=True):
        m = _FENCE_LINE_RE.match(line)
        if fence is None and m:
            fence = (pos, m.group(1)[0], len(m.group(1)))
        elif fence is not None:
            if m and m.group(1)[0] == fence[1] and len(m.group(1)) >= fence[2] and not line[m.end():].strip():
                spans.append((fence[0], pos + len(line)))
                fence = None
        elif line.startswith(("    ", "\t")):
            spans.append((pos, pos + len(line)))
        pos += len(line)
    if fence is not None:            # an unclosed fence runs to the end (CommonMark)
        spans.append((fence[0], len(text)))
    return spans


def normalise_refs(text: str, claims: dict[str, dict]) -> str:
    """v2 write-up (hb2, tb R2): an inline code span whose whole content is a known claim reference, or a known
    claim id (one with '.' or '_', as report_claim_id_raw reads ids), is that reference: `[CLAIM:x]` or `x` becomes
    [CLAIM:x], which render_text then renders. An id outside the ledger, a longer span and code blocks are left
    as written, so each is still reported."""
    blocks = _code_block_lines(text)

    def _sub(m: re.Match) -> str:
        if in_spans(blocks, m.start()):
            return m.group(0)
        inner = m.group(2).strip()
        ref = _CLAIM_REF_RE.fullmatch(inner)
        cid = ref.group(1).strip() if ref else inner
        known = cid in claims and bool(_CLAIM_ID_RE.fullmatch(cid))
        return f"[CLAIM:{cid}]" if known and (ref or "." in cid or "_" in cid) else m.group(0)
    return _INLINE_CODE_RE.sub(_sub, text)


def quote_refs(text: str, claims: dict[str, dict], render: bool = True) -> str:
    """Text the harness quotes from a finding (its claim or remedy) holds no claim reference outside the ledger:
    hb2 (measles R1, dengue R2) quoted a critic's [CLAIM:x] and placeholder [CLAIM:id] into report.md, and the
    writer repeated the placeholder from declared_findings.yaml. A reference outside the ledger becomes the words
    `claim x`; a ledger one is rendered as its value (`render`, for report.md) or kept as a reference the writer may
    use (for the writer's inputs). Substitution repeats until nothing changes (a nested marker), then any other
    `[CLAIM:` (unclosed, across lines, rebuilt) loses its bracket. Code spans are no exception: what the harness
    quotes is never syntax. Nothing else is rewritten: a bare id stays as recorded."""
    kept: list[str] = []       # final text, put back last: a rendered value is never substituted again

    def _sub(m: re.Match) -> str:
        cid = m.group(1).strip()
        c = claims.get(cid) if _CLAIM_ID_RE.fullmatch(cid or "") else None
        if c is None:
            return f"claim {cid or '(no id)'}"
        try:
            kept.append(f"[CLAIM:{cid}]" if not render else render_claim(c).replace("[CLAIM:", "CLAIM:"))
        except Exception:  # noqa: BLE001 -- a claim that cannot render is named, never left as a reference
            return f"claim {cid}"
        return f"\x00{len(kept) - 1}\x01"
    out, prev = text.replace("\x00", "").replace("\x01", ""), None
    for _ in range(8):                 # bounded: a label value could itself hold a marker
        if out == prev:
            break
        prev, out = out, _QUOTED_REF_RE.sub(_sub, out)
    out = out.replace("[CLAIM:", "CLAIM:")
    return re.sub("\x00(\\d+)\x01", lambda m: kept[int(m.group(1))], out)


def _builder():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "build_claims_ledger", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "build_claims_ledger.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Inlined declared-limits expansion (from declared_limits.py)
# ---------------------------------------------------------------------------

DECLARED_LIMITS_TOKEN = "[DECLARED_LIMITS]"
_DL_BEGIN = "<!-- declared-limits: generated by the harness from scope_declaration.yaml -->"
_DL_END = "<!-- /declared-limits -->"
_DL_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_DL_FENCE_RE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[^\n]*$", re.S | re.M)
_DL_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_DL_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$", re.M)
_DL_BODY_TITLE_RE = re.compile(r"(?:\d|introduction\b|background\b|methods?\b)", re.I)
_DL_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>\n]*>")
_DL_MD_SPECIAL_RE = re.compile(r"([\\`*_\[\]#|~])")


class DeclaredLimitsError(RuntimeError):
    """scope_declaration.yaml cannot be read: the block cannot be rendered."""


def _dl_declarations(run_dir: str) -> list[dict]:
    doc, entries, findings = sd.load(run_dir)
    if doc is None and findings:
        raise DeclaredLimitsError("; ".join(str(f.get("claim") or f.get("kind")) for f in findings)[:400])
    return [e for e in entries if isinstance(e, dict) and sd.entry_has_justification(e)]


def _dl_one_line(x, limit: int = 600) -> str:
    s = " ".join(str(x or "").split())
    s = s if len(s) <= limit else s[:limit - 1].rstrip() + "…"
    return _DL_MD_SPECIAL_RE.sub(r"\\\1", html.escape(s, quote=False))


def _dl_effect(run_dir: str, entry: dict) -> str:
    if entry.get("decision_test") is not None:
        res, err = sd.declaration_test(run_dir, entry)
        if err or not isinstance(res, dict):
            return f"invalid test ({_dl_one_line(err or 'no result', 200)})"
        rec = res.get("recommendation") if isinstance(res.get("recommendation"), dict) else {}
        verdict = "changes the recommendation" if res.get("decision_changes") else \
            "does not change the recommendation"
        return (f"tested ({_dl_one_line(entry.get('decision_test'), 120)}): {verdict}; "
                f"with: {_dl_one_line(rec.get('with'), 200)}; without: {_dl_one_line(rec.get('without'), 200)}")
    reason = sd.exemption(run_dir, entry)
    if reason:
        return f"exempt: {_dl_one_line(reason, 300)}"
    return "untested"


def render_declared_limits_block(run_dir: str) -> str:
    ents = _dl_declarations(run_dir)
    lines = [_DL_BEGIN, "**What this does not establish.** The run declared these limits "
                        "(scope_declaration.yaml; full text in Limitations):", ""]
    if not ents:
        lines.append("- No limitation was declared.")
    for e in ents:
        did = _dl_one_line(e.get("id") or "(no id)", 40)
        claim = _dl_one_line(e.get("claim") or e.get("description") or e.get("why_unresolved"))
        lines.append(f"- **{did}**: {claim} Effect on the recommendation: {_dl_effect(run_dir, e)}.")
    lines.append(_DL_END)
    return "\n".join(lines)


def _dl_visible(text: str) -> str:
    blank = lambda m: re.sub(r"[^\n]", " ", m.group(0))  # noqa: E731
    return _DL_INLINE_CODE_RE.sub(blank, _DL_FENCE_RE.sub(blank, _DL_COMMENT_RE.sub(blank, text)))


def _dl_first_body(vis: str) -> Optional[re.Match]:
    for n, m in enumerate(_DL_HEADING_RE.finditer(vis)):
        if n == 0 and len(m.group(1)) == 1:
            continue
        if _DL_BODY_TITLE_RE.match(m.group(2).strip("*_ ")):
            return m
    return None


def expand_declared_limits(run_dir: str, text: str) -> str:
    vis = _dl_visible(text)
    spots = [m.start() for m in re.finditer(re.escape(DECLARED_LIMITS_TOKEN), vis)]
    if not spots:
        return text
    block = render_declared_limits_block(run_dir)
    for pos in reversed(spots):
        text = text[:pos] + block + text[pos + len(DECLARED_LIMITS_TOKEN):]
    return text


def _dl_read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _declared_limits_problems(run_dir: str) -> list[str]:
    draft = _dl_read(os.path.join(run_dir, "report.unrendered.md"))
    if draft is None:
        draft = _dl_read(os.path.join(run_dir, "manuscript.md"))
    report = _dl_read(os.path.join(run_dir, "report.md"))
    if draft is None and report is None:
        return []
    try:
        if not _dl_declarations(run_dir):
            return []
    except DeclaredLimitsError as e:
        return [f"scope_declaration.yaml cannot be read, so the declared limits cannot be rendered ({e})"]
    out = []
    if draft is None:
        out.append("there is no writer's draft (manuscript.md) to place the token in")
    else:
        vis = _dl_visible(draft)
        spots = [m.start() for m in re.finditer(re.escape(DECLARED_LIMITS_TOKEN), vis)]
        body = _dl_first_body(vis)
        if len(spots) != 1:
            out.append(f"the draft has the token {DECLARED_LIMITS_TOKEN} {len(spots)} times outside comments and code "
                       "(it must appear exactly once)")
        else:
            pos = spots[0]
            line_start = vis.rfind("\n", 0, pos) + 1
            line_end = vis.find("\n", pos)
            line = vis[line_start:line_end if line_end >= 0 else len(vis)]
            if line.rstrip() != DECLARED_LIMITS_TOKEN:
                out.append(f"the token {DECLARED_LIMITS_TOKEN} is not on its own line (nothing before or after it on the line, "
                           "no quote or list marker)")
            if body is not None and pos > body.start():
                out.append(f"the token {DECLARED_LIMITS_TOKEN} comes after the first body section "
                           f"(`{body.group(0).strip()[:60]}`); put it in the executive summary")
            tags = _DL_HTML_TAG_RE.findall(vis[:pos])
            if tags:
                out.append(f"raw HTML before the token ({', '.join(t[:30] for t in tags[:3])}): the executive "
                           "summary may not contain HTML, which can hide the rendered block")
    if report is not None:
        try:
            if render_declared_limits_block(run_dir) not in report:
                out.append("report.md does not contain the declared-limits block as it renders now (re-run "
                           "scripts/render_claims.py after the last change to the draft or the declarations)")
        except DeclaredLimitsError as e:
            out.append(f"the declared-limits block cannot be rendered ({e})")
    return out


# ---------------------------------------------------------------------------
# Inlined percentage-vs-proportion claim unit check (from claim_units.py)
# ---------------------------------------------------------------------------

_PCT_TOKENS = frozenset({"pct", "percent", "percentage", "percentages", "perc", "pc", "pp", "pctpt", "ppt"})
_PROP_TOKENS = frozenset({"share", "shares", "prop", "proportion", "proportions", "frac", "fraction",
                          "fractions", "prob", "probability", "p", "pr"})
_STAT_KEYS = frozenset({"mean", "median", "value", "point", "estimate", "lo", "hi", "low", "high",
                        "lower", "upper", "ci_low", "ci_high", "ci_lower", "ci_upper"})
_PROP_UNITS = re.compile(r"\b(proportion|fraction|share|probability|0\s*[-–]\s*1|unit interval)\b", re.I)
_PCT_UNITS = re.compile(r"%|\bper\s*cent\b|\bpercent", re.I)


def _cu_tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", str(name).lower()) if t]


def _cu_pct_marked(name: str) -> bool:
    if "%" in str(name):
        return True
    return any(t in _PCT_TOKENS or t.endswith("pct") or t.startswith("pct") for t in _cu_tokens(name))


def _cu_prop_named(name: str) -> bool:
    return any(t in _PROP_TOKENS or re.fullmatch(r"p\d+", t) for t in _cu_tokens(name))


def _cu_snum(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    try:
        return P._num(x)
    except OverflowError:
        return math.inf
    except (ValueError, TypeError):
        return None


def _cu_num_items(node: Any) -> dict[str, float]:
    if not isinstance(node, dict):
        return {}
    out = {}
    for k, v in node.items():
        n = _cu_snum(v)
        if n is not None:
            out[str(k)] = n
    return out


def _cu_unit(xs) -> bool:
    return all(0 <= x <= 1 for x in xs)


def _cu_inside(xs) -> int:
    return sum(1 for x in xs if 0 < x < 1)


def _cu_ci_nums(ci) -> list[float]:
    if not isinstance(ci, (list, tuple)):
        return []
    return [x for x in (_cu_snum(e) for e in ci) if x is not None]


def _cu_scale_evidence(c: dict, v: float, run_dir: str, cache: dict) -> tuple[list[str], str] | None:
    src, field = str(c.get("source_artifact") or ""), str(c.get("source_field") or "")
    path = P._resolve_artifact_path(run_dir, src)
    if path is None or not field:
        return None
    own = [v] + [x for x in (_cu_snum(c.get("ci_low")), _cu_snum(c.get("ci_high"))) if x is not None]
    try:
        point, ci = P.resolve_claim(c, run_dir, cache)
    except P.ClaimUnresolved:
        return None
    sv = _cu_snum(point)
    if sv is None or not math.isclose(v, sv, rel_tol=0.01, abs_tol=1e-12):
        return None
    own += [sv] + _cu_ci_nums(ci)
    ev: list[str] = []
    units = str(c.get("units") or "")
    if _PROP_UNITS.search(units) and not _PCT_UNITS.search(units):
        ev.append(f"units {units!r} name a proportion")
    ext = os.path.splitext(path)[1].lower()
    if ext in (".yaml", ".yml", ".json"):
        segs = [s for s in re.split(r"\.|\[[^\]]*\]", field) if s]
        if not segs or any(_cu_pct_marked(s) for s in segs):
            return None
        stat = segs[-1].lower() in _STAT_KEYS and len(segs) >= 2
        qname = segs[-2] if stat else segs[-1]
        try:
            if path not in cache:
                cache[path] = P._load_structured(path)
            doc = cache[path]
            qpath = field[: -len(segs[-1])].rstrip(".") if stat and field.endswith(segs[-1]) else field
            ppath = qpath[: -len(qname)].rstrip(".") if qpath.endswith(qname) else None
            qnode = P.resolve_dotpath(doc, qpath) if stat else None
            parent = (P.resolve_dotpath(doc, ppath) if ppath else doc) if ppath is not None else None
        except (P._Unresolved, OSError, ValueError, UnicodeDecodeError, yaml.YAMLError):
            return (ev, qname) if ev and _cu_unit(own) else None
        for node in (qnode, P.resolve_dotpath(doc, field) if not stat else None):
            if isinstance(node, dict):
                own += _cu_ci_nums(P._ci_from_mapping(node))
        if not _cu_unit(own):
            return None
        sib = {k: x for k, x in _cu_num_items(parent).items() if k != qname}
        marked_big = sorted(k for k, x in sib.items() if _cu_pct_marked(k) and abs(x) > 1)
        if marked_big:
            ev.append(f"the mapping that holds it names its percents and writes them on the 0-100 "
                      f"scale ({marked_big[0]} = {sib[marked_big[0]]:.4g}); this field is not named as one")
        elif sib and not any(_cu_pct_marked(k) for k in sib) and _cu_unit(sib.values()) \
                and _cu_inside(sib.values()) >= 2:
            ev.append(f"all {len(sib)} numeric values beside it are in [0, 1], "
                      f"{_cu_inside(sib.values())} strictly inside")
    elif ext == ".csv":
        f2 = field
        fm = P._CSV_FILTER_PREFIX_RE.match(f2) or P._CSV_FILTER_SUFFIX_RE.match(f2)
        if fm:
            f2 = fm.group("rest")
        m = P._CSV_AGG_RE.match(f2)
        col = (m.group("col") or "").strip() if m else ""
        if not col or m.group("agg") in ("count", "nunique", "len") or _cu_pct_marked(col):
            return None
        qname = col
        try:
            with open(path, encoding="utf-8", newline="") as fh:
                vals = [P._num(r.get(col)) for r in csv.DictReader(fh)]
        except (OSError, UnicodeDecodeError, ValueError, csv.Error):
            return (ev, qname) if ev and _cu_unit(own) else None
        vals = [x for x in vals if x is not None]
        if not _cu_unit(own + vals):
            return None
        if _cu_inside(vals) >= 2:
            ev.append(f"all {len(vals)} values of column {col!r} are in [0, 1], {_cu_inside(vals)} strictly inside")
    else:
        qname = field
        if _cu_pct_marked(field) or not _cu_unit(own):
            return None
    return (ev, qname) if ev else None


def claim_unit_problems(claims: list, run_dir: str) -> list[dict]:
    out, cache = [], {}
    for c in claims or []:
        if not isinstance(c, dict) or str(c.get("claim_kind") or "").lower() != "percentage":
            continue
        if P.is_external(c.get("source_artifact")):
            continue
        v = _cu_snum(c.get("value"))
        if v is None or not 0 < v <= 1:
            continue
        try:
            found = _cu_scale_evidence(c, v, run_dir, cache)
        except Exception as e:  # noqa: BLE001
            out.append({"id": c.get("id"), "value": v, "unchecked": True,
                        "rendered": "?", "as_percent": "?", "name_hint": False,
                        "evidence": [f"could not be checked: {type(e).__name__}: {e}"[:200]],
                        "source": f"{c.get('source_artifact')}::{c.get('source_field')}"})
            continue
        if not found:
            continue
        ev, qname = found
        out.append({"id": c.get("id"), "value": v, "rendered": f"{v:.1f}%".replace(".0%", "%"),
                    "as_percent": f"{v * 100:.1f}%".replace(".0%", "%"), "evidence": ev,
                    "name_hint": _cu_prop_named(qname),
                    "source": f"{c.get('source_artifact')}::{c.get('source_field')}"})
    return out


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

    limits_error: list[str] = []

    def _write_report(body):
        # Phase 49 γ (A22): the harness writes the declared limits in place of the
        # writer's [DECLARED_LIMITS] token. Unreadable declarations: the token stays,
        # the render reports it, the gate flags it.
        try:
            body = expand_declared_limits(run_dir, body)
        except DeclaredLimitsError as e:
            limits_error.append(f"declared limits not rendered: {e}")
        from lib.safe_io import publish                 # no link followed (follow-up to review round 5)
        publish(report_path, body, run_dir)

    protected = _fenced_or_inline_code_spans(text)
    n_refs = sum(1 for m in _CLAIM_REF_RE.finditer(text)
                 if not _in_protected_span(protected, m.start()))
    if n_refs == 0:
        # No references: the draft is the report
        snap = None
        if source_path != report_path:
            if DECLARED_LIMITS_TOKEN in text:
                # Codex round 2: the expanded report has an unrendered snapshot even
                # with no claim references, so the gate reads the draft it came from.
                snap = os.path.join(run_dir, "report.unrendered.md")
                from lib.safe_io import publish
                publish(snap, open(source_path, "rb").read(), run_dir)
            _write_report(text)
        out = {"ok": not limits_error, "n_refs": 0, "n_unresolved": 0,
               "unresolved_ids": [], "snapshot_path": snap,
               "report_path": report_path}
        if limits_error:
            out["error"] = limits_error[0]
        return out

    if os.path.exists(os.path.join(run_dir, "models", "claims_spec.yaml")):
        _, spec_errors = _builder().write(run_dir)
        if spec_errors:
            return {"ok": False, "error": "claims_spec.yaml: " + "; ".join(spec_errors[:5]),
                    "n_refs": n_refs, "n_unresolved": 0, "unresolved_ids": []}
    claims = load_claims(ledger_path)

    # Snapshot the writer's draft before substitution.
    snapshot_path = os.path.join(run_dir, "report.unrendered.md")
    from lib.safe_io import publish
    publish(snapshot_path, open(source_path, "rb").read(), run_dir)

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
    if limits_error:
        return {"ok": False, "error": limits_error[0], "n_refs": n_refs, "n_unresolved": 0,
                "unresolved_ids": [], "snapshot_path": snapshot_path, "report_path": report_path}
    # Phase 47 β (A18): percentage claims cited here whose source holds a
    # proportion (rendered 100 times too small). Reported, not blocking: the
    # gate raises them (claim_percentage_is_proportion).
    try:
        cited = {m.group(1).strip() for m in _CLAIM_REF_RE.finditer(text)}
        scale = [p for p in claim_unit_problems(list(claims.values()), run_dir)
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
    s = render_claim({"id": "x", "claim_kind": "scalar", "value": 7467997, "ci_low": 5140000, "ci_high": 10420000,
                      "interval": {"level": 0.95, "covers": ["engine_parameters"], "excludes": ["costs"]}})
    ok("(95% UI over engine parameters; not covered: costs: 5.14M" in s and "CI" not in s,
       f"T1b v2 lesson 6: the interval label is derived from structured metadata: {s!r}")
    lab = interval_label({"level": 0.95, "covers": ["engine_parameters", "priors"], "excludes": ["costs"],
                          "prior_dominated": ["smc_pe", "dw_5_14"], "fixed": ["cm_add"], "fixed_other": 3})
    ok(lab == "95% UI over engine parameters, priors; set by declared ranges, not by data: smc_pe, dw_5_14; held fixed: "
              "cm_add and 3 other engine parameters; not covered: costs",
       f"T1d v2 priors: the label names the prior-dominated and the fixed parameters: {lab!r}")
    try:
        lab = interval_label({"level": 0.95, "covers": ["priors"], "excludes": [], "prior_dominated": ["use_rate"],
                              "spec_fixed": ["retention_half"], "fixed": ["cm_add"]})
    except ValueError as e:
        lab = f"refused: {e}"
    ok(lab == "95% UI over priors; set by declared ranges, not by data: use_rate; held fixed at the spec value: "
              "retention_half; held fixed: cm_add",
       f"T1e spec-declared engine parameter values held fixed are named as such in the label: {lab!r}")
    ok(interval_meta_problem({"level": 0.95, "covers": ["priors"], "spec_fixed": ["held at 0.75 y"]}) is not None,
       "T1f the spec-value list holds parameter identifiers only (no free text)")
    try:      # review round 2 (R2-2): the engine contract's parameter names (compartmental, package-rate engines)
        lab = interval_label({"level": 0.95, "covers": ["priors"], "prior_dominated": ["efficacy:vax"],
                              "fixed": ["lrr:a+b"], "spec_fixed": ["coverage:routine"]})
    except ValueError as e:
        lab = f"refused: {e}"
    ok(lab == "95% UI over priors; set by declared ranges, not by data: efficacy:vax; held fixed at the spec value: "
              "coverage:routine; held fixed: lrr:a+b",
       f"T1g parameter names with ':' and '+' are identifiers the label accepts: {lab!r}")
    ok(all(interval_meta_problem({"level": 0.95, "covers": ["priors"], "fixed": [bad]}) is not None
           for bad in ("held at 3%", "a b", "x;y", "", ":lead", "a" * 90)),
       "T1h free text is still refused (spaces, punctuation outside the contract's names, empty, overlong)")
    with tempfile.TemporaryDirectory() as d_il:
        for bad_c, why in (({"interval_label": "95% UI over everything"}, "free text"),
                           ({"interval": {"level": 0.95, "covers": ["costs"], "fixed": ["held at 3%"]}},
                            "free text in fixed"),
                           ({"interval": {"level": 0.95, "covers": ["all of it, 100%"]}}, "free text in covers"),
                           ({"interval": {"level": 2, "covers": ["costs"]}}, "a bad level")):
            pth = os.path.join(d_il, "l.yaml")
            yaml.safe_dump({"claims": [dict({"id": "h", "claim_kind": "scalar", "value": 2, "ci_low": 1,
                                             "ci_high": 3}, **bad_c)]}, open(pth, "w"))
            try:
                load_claims(pth)
                ok(False, f"T1c a ledger interval with {why} is refused (Codex P1-5)")
            except ValueError:
                pass
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
        # T7c (Phase 49 γ, A22): the [DECLARED_LIMITS] token becomes the harness-written block.
        with open(os.path.join(d, "scope_declaration.yaml"), "w") as f:
            yaml.safe_dump({"declarations": [{"id": "SD-007", "blocker_id": "allocation_unstable",
                                              "claim": "Allocation unstable out of sample.",
                                              "why_unresolved": "w"}]}, f)
        with open(os.path.join(d, "manuscript.md"), "w") as f:
            f.write("# Report\n\n## Executive summary\n\nAverts [CLAIM:dalys] DALYs.\n\n[DECLARED_LIMITS]\n\n"
                    "## 1. Introduction\n\n```\n[DECLARED_LIMITS]\n```\n")
        render_run(d)
        rendered = open(os.path.join(d, "report.md")).read()
        ok("**SD-007**: Allocation unstable out of sample." in rendered and "7.47M" in rendered
           and rendered.count("[DECLARED_LIMITS]") == 1 and "[DECLARED_LIMITS]" in rendered.split("```")[1],
           f"T7c the token is replaced by the declared-limits block (not inside code): {rendered[:600]}")
        # T7d (Codex round 2): with no claim references, the expanded report still gets a
        # fresh unrendered snapshot.
        with open(os.path.join(d, "manuscript.md"), "w") as f:
            f.write("# Report\n\n## Executive summary\n\nNo numbers.\n\n[DECLARED_LIMITS]\n\n## 1. Introduction\n")
        r = render_run(d)
        ok(r["ok"] and "No numbers." in open(os.path.join(d, "report.unrendered.md")).read()
           and "**SD-007**" in open(os.path.join(d, "report.md")).read(),
           f"T7d zero references: report.unrendered.md is refreshed and the block rendered: {r}")
        # T7e (Codex round 2): unreadable declarations: a controlled render failure.
        with open(os.path.join(d, "scope_declaration.yaml"), "w") as f:
            f.write("declared_at: 2026-13-45\ndeclarations: []\n")
        r = render_run(d)
        ok(not r["ok"] and "declared limits not rendered" in r.get("error", "")
           and "[DECLARED_LIMITS]" in open(os.path.join(d, "report.md")).read(),
           f"T7e unreadable scope_declaration.yaml: render fails with a reason, token left: {r}")

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

    # RCL_ISOLATE (v2 autonomy 1A, 6.2).
    with tempfile.TemporaryDirectory() as d:
        lp = os.path.join(d, "l.yaml")
        with open(lp, "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "a.good", "claim_kind": "scalar", "value": 5},
                {"id": "b.ci", "claim_kind": "scalar", "value": 5, "ci_low": 6, "ci_high": 7},
                {"id": "c.dup", "claim_kind": "count", "value": 1}, {"id": "c.dup", "claim_kind": "count", "value": 2},
                "not a mapping"]}, f)
        try:
            got, bad = globals()["load_claims_tolerant"](lp)
        except Exception as e:  # noqa: BLE001 -- before the reader exists this is the failure
            got, bad = {}, {"raised": f"{type(e).__name__}: {e}"}
        try:
            load_claims(lp)
            strict = "accepted"
        except ValueError:
            strict = "refused"
        ok(set(got) == {"a.good"} and set(bad) == {"b.ci", "c.dup", "claims[4]"} and "claims[1]" in bad.get("b.ci", "")
           and strict == "refused",
           f"RCL_ISOLATE the tolerant reader keeps the valid claim and names each invalid one; load_claims still "
           f"refuses: {sorted(got)}, {bad}, strict {strict}")
        with open(lp, "w") as f:
            f.write("claims: 3\n")
        try:
            globals()["load_claims_tolerant"](lp)
            whole = "accepted"
        except ValueError:
            whole = "refused"
        except Exception as e:  # noqa: BLE001
            whole = type(e).__name__
        ok(whole == "refused", f"RCL_ISOLATE2 a ledger that is not a list of claims is still refused whole ({whole})")
        # RCL_ISOLATE3 (narrow Codex review of #297, 1): a claim whose fields have the wrong type costs that claim.
        with open(lp, "w") as f:
            yaml.safe_dump({"claims": [{"id": "a.good", "claim_kind": "scalar", "value": 5},
                                       {"id": "d.kind", "claim_kind": []}]}, f)
        try:
            got, bad = globals()["load_claims_tolerant"](lp)
        except Exception as e:  # noqa: BLE001
            got, bad = {}, {"raised": f"{type(e).__name__}: {e}"}
        ok(set(got) == {"a.good"} and set(bad) == {"d.kind"},
           f"RCL_ISOLATE3 a claim with an unhashable kind is one invalid claim ({sorted(got)}, {bad})")
        # RCL_ISOLATE4-5 (round 2, 10-11).
        with open(lp, "w") as f:
            yaml.safe_dump({"claims": [None, {"id": "claims[0]"}]}, f)
        try:
            _, bad = globals()["load_claims_tolerant"](lp)
        except Exception as e:  # noqa: BLE001
            bad = {"raised": f"{type(e).__name__}: {e}"}
        ok(set(bad) == {"claims[0]", "claims[1]"}, f"RCL_ISOLATE4 each invalid row has its own entry ({sorted(bad)})")
        with open(lp, "w") as f:
            f.write("- 1\n")
        raised = []
        for fn in (load_claims, globals().get("load_claims_tolerant")):
            try:
                fn(lp)
                raised.append(None)
            except Exception as e:  # noqa: BLE001
                raised.append(type(e).__name__)
        ok(raised == ["AttributeError", "ValueError"],
           f"RCL_ISOLATE5 load_claims is unchanged for a ledger that is a list; the tolerant reader refuses it ({raised})")

    # CE3_RCL: planted .icer claim refused when strategy status is dominated/degenerate, negative, or carries interval
    with tempfile.TemporaryDirectory() as d:
        lp = os.path.join(d, "cea.yaml")
        for bad_name, rows in (
            ("dominated_icer", [
                {"id": "cea.health_system.s1.status", "claim_kind": "label", "value": "dominated"},
                {"id": "cea.health_system.s1.icer", "claim_kind": "currency", "value": 500.0},
            ]),
            ("sw_icer", [
                {"id": "cea.health_system.s2.status", "claim_kind": "label", "value": "less_health_lower_cost"},
                {"id": "cea.health_system.s2.icer", "claim_kind": "currency", "value": 100.0},
            ]),
            ("negative_icer", [
                {"id": "cea.health_system.s3.icer", "claim_kind": "currency", "value": -50.0},
            ]),
            ("interval_icer", [
                {"id": "cea.health_system.s4.icer", "claim_kind": "currency", "value": 250.0,
                 "ci_low": 100.0, "ci_high": 400.0},
            ]),
        ):
            with open(lp, "w") as f:
                yaml.safe_dump({"claims": rows}, f)
            strict_bad = False
            try:
                load_claims(lp)
            except ValueError:
                strict_bad = True
            good_t, bad_t = load_claims_tolerant(lp)
            ok(strict_bad and any(k.endswith(".icer") for k in bad_t) and not any(k.endswith(".icer") for k in good_t),
               f"CE3_RCL {bad_name}: planted .icer refused by load_claims and load_claims_tolerant ({bad_t})")
        with open(lp, "w") as f:
            yaml.safe_dump({"claims": [
                {"id": "cea.health_system.s_ok.status", "claim_kind": "label", "value": "frontier"},
                {"id": "cea.health_system.s_ok.icer", "claim_kind": "currency", "value": 350.0,
                 "units": "USD (2025) per DALY averted"},
            ]}, f)
        ok_map = load_claims(lp)
        ok("cea.health_system.s_ok.icer" in ok_map, "CE3_RCL valid frontier .icer accepted")

    # Inlined claim_units tests (U1-U3, U9-U11)
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        w = lambda rel, obj: yaml.safe_dump(obj, open(os.path.join(d, rel), "w"))
        w("models/derived.yaml", {
            "as_delivered": {"cost_m": 250.4, "gain_topped_up_pct": 10.37, "gain_topped_up_psa_lo": 6.3,
                             "gain_topped_up_p10": 0.587, "small_change_pct": 0.5},
            "r057": {"primary_gain_pct": 13.27, "psa_within": {"mean": 0.876, "ci_low": -4.44, "ci_high": 6.96}},
            "drv": {"net_prices": 0.0, "residual": 0.8333, "case_load": 0.0},
            "coverage": {"region_a": 0.42, "region_b": 0.61, "region_c": 0.35},
            "plan": {"share_prog": 0.4, "cost_m": 120.0, "n_units": 30},
            "growth": {"value": 0.3},
        })
        open(os.path.join(d, "models", "units.csv"), "w").write(
            "unit,frac_reached,reached_pct\nA,0.2,20\nB,0.6,60\nC,0.9,90\n")
        spec = {"claims": [
            {"id": "p10", "claim_kind": "percentage", "source_artifact": "models/derived.yaml",
             "source_field": "as_delivered.gain_topped_up_p10"},
            {"id": "real_half", "claim_kind": "percentage", "source_artifact": "models/derived.yaml",
             "source_field": "as_delivered.small_change_pct"},
            {"id": "ci_cross", "claim_kind": "percentage", "source_artifact": "models/derived.yaml",
             "source_field": "r057.psa_within.mean"},
            {"id": "zeros", "claim_kind": "percentage", "source_artifact": "models/derived.yaml",
             "source_field": "drv.residual"},
            {"id": "cov_a", "claim_kind": "percentage", "source_artifact": "models/derived.yaml",
             "source_field": "coverage.region_a"},
            {"id": "share_only", "claim_kind": "percentage", "source_artifact": "models/derived.yaml",
             "source_field": "plan.share_prog"},
            {"id": "units_prop", "claim_kind": "percentage", "units": "proportion",
             "source_artifact": "models/derived.yaml", "source_field": "growth.value"},
            {"id": "csv_frac", "claim_kind": "percentage", "source_artifact": "models/units.csv",
             "source_field": "frac_reached.mean()"},
            {"id": "csv_pct", "claim_kind": "percentage", "source_artifact": "models/units.csv",
             "source_field": "reached_pct.min()"},
            {"id": "big", "claim_kind": "percentage", "source_artifact": "models/derived.yaml",
             "source_field": "as_delivered.gain_topped_up_pct"},
            {"id": "scalar_p", "claim_kind": "scalar", "units": "probability",
             "source_artifact": "models/derived.yaml", "source_field": "as_delivered.gain_topped_up_p10"},
            {"id": "ext", "claim_kind": "percentage", "value": 0.8, "source_artifact": "external",
             "source_field": "a published case-fatality ratio (PMID 0)"},
        ]}
        w("models/claims_spec.yaml", spec)
        doc, errs = _builder().write(d)
        probs = {p["id"]: p for p in claim_unit_problems(doc["claims"], d)}
        ok(errs == [] and set(probs) == {"p10", "cov_a", "units_prop", "csv_frac"},
           f"U1 flags proportions with source evidence: {sorted(probs)} {errs}")
        p = probs.get("p10") or {}
        ok(p.get("rendered") == "0.6%" and p.get("as_percent") == "58.7%" and p.get("name_hint") is True
           and "gain_topped_up_pct = 10.37" in " ".join(p.get("evidence", [])),
           f"U2 proportion evidence names marked sibling: {p}")
        ok(not probs.get("cov_a", {}).get("name_hint") and "csv_frac" in probs and "csv_pct" not in probs,
           f"U3 all-in-unit siblings and CSV column: {probs}")

    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        yaml.safe_dump({
            "a": {"gain_pct": 10.4, "p_small": 0.005, "p10": 0.587},
            "g": {"gain_pct": 13.3,
                  "within": {"mean": 0.12, "ci_95": [-0.2, 0.48]},
                  "share": {"mean": 0.12, "ci_95": [0.05, 0.3]}},
        }, open(os.path.join(d, "models", "s.yaml"), "w"))
        mk = lambda cid, field, v: {"id": cid, "claim_kind": "percentage", "value": v,
                                    "source_artifact": "models/s.yaml", "source_field": field}
        got = {p["id"] for p in claim_unit_problems([
            mk("converted", "a.p_small", 0.5),
            mk("as_source", "a.p_small", 0.005),
            mk("mismatch", "a.p10", 0.3),
            mk("ci_neg", "g.within.mean", 0.12),
            mk("ci_unit", "g.share.mean", 0.12),
            mk("unresolved", "a.nope", 0.4),
        ], d)}
        ok(got == {"as_source", "ci_unit"}, f"U9 converted value and zero-crossing CI exempt: {sorted(got)}")

    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        yaml.safe_dump({"g": {"gain_pct": 13.3,
                              "share": {"mean": 0.12, "ci_low": 0.05, "ci_high": 0.3, "n": 1000},
                              "huge": {"mean": 0.12, "ci_95": [-(10 ** 400), 0.3]}}},
                       open(os.path.join(d, "models", "s.yaml"), "w"))
        mk = lambda cid, field, v, **kw: {"id": cid, "claim_kind": "percentage", "value": v,
                                          "source_artifact": "models/s.yaml", "source_field": field, **kw}
        claims_u10 = [mk("with_n", "g.share.mean", 0.12),
                      mk("huge_src", "g.huge.mean", 0.12),
                      mk("huge_claim", "g.share.mean", 0.12, ci_low=-(10 ** 400), ci_high=0.3),
                      mk("after", "g.share.mean", 0.12)]
        got_u10 = {p["id"]: p for p in claim_unit_problems(claims_u10, d)}
        ok(set(got_u10) == {"with_n", "after"} and not any(p.get("unchecked") for p in got_u10.values()),
           f"U10 metadata beside stat and overflowing endpoint: {sorted(got_u10)}")
        claims2 = [{"id": "bad", "claim_kind": "percentage", "value": 0.5,
                    "source_artifact": "models/s.yaml", "source_field": object()}, mk("after", "g.share.mean", 0.12)]
        real = P.resolve_claim
        P.resolve_claim = lambda c, *a, **k: (_ for _ in ()).throw(RuntimeError("boom")) \
            if c["id"] == "bad" else real(c, *a, **k)
        try:
            got2 = {p["id"]: p for p in claim_unit_problems(claims2, d)}
        finally:
            P.resolve_claim = real
        ok(got2.get("bad", {}).get("unchecked") and "after" in got2 and not got2["after"].get("unchecked"),
           f"U11 one failing claim reported unchecked and rest checked: {got2}")

    # Inlined declared_limits tests (DL1-DL23b)
    with tempfile.TemporaryDirectory() as d:
        def wd(rel, obj):
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(obj) if isinstance(obj, str) else yaml.safe_dump(obj, fh)

        def draft(text):
            wd("manuscript.md", text)
            rp = os.path.join(d, "report.unrendered.md")
            if os.path.exists(rp):
                os.remove(rp)
            wd("report.md", expand_declared_limits(d, text))

        decl = {"declarations": [
            {"id": "SD-007", "blocker_id": "allocation_unstable",
             "claim": "Leave-one-out allocation is UNSTABLE.", "why_unresolved": "w"},
            {"id": "SD-002", "blocker_id": "benchmark_drift", "claim": "B15 interaction not reproduced.",
             "why_unresolved": "w", "decision_test": "models/sufficiency/sd002.yaml"}]}
        head = "# Title\n\n## Executive summary\n\nFund program.\n\n"
        body = "\n\n## 1. Introduction\n\nText.\n\n## 8. Limitations\n\nSD-007 SD-002 verbatim.\n"
        ok(_declared_limits_problems(d) == [], "DL1 no draft: nothing")
        wd("manuscript.md", head + body)
        ok(_declared_limits_problems(d) == [], "DL2 no declarations: token not required")
        wd("scope_declaration.yaml", decl)
        p_dl3 = _declared_limits_problems(d)
        ok(len(p_dl3) == 1 and "0 times" in p_dl3[0], f"DL3 declarations and no token: {p_dl3}")
        draft(head + DECLARED_LIMITS_TOKEN + body)
        rep = open(os.path.join(d, "report.md")).read()
        ok(_declared_limits_problems(d) == [] and "**SD-007**: Leave-one-out allocation is UNSTABLE." in rep
           and "**SD-002**" in rep and DECLARED_LIMITS_TOKEN not in rep, "DL4 token rendered")
        ok("Effect on the recommendation: untested" in rep, "DL5 untested rendered")
        ok("invalid test" in rep and "SD-002" in rep, "DL6 missing test file renders invalid test")
        wd("models/sufficiency/sd002.yaml", {"finding": "benchmark_drift", "varied": "interaction OR",
                                             "criterion": "package changes in >10% of units",
                                             "decision_changes": False,
                                             "recommendation": {"with": "no opt B", "without": "no opt B"}})
        ok(any("does not contain the declared-limits block" in x for x in _declared_limits_problems(d)),
           "DL7 stale block after test change")
        draft(head + DECLARED_LIMITS_TOKEN + body)
        rep = open(os.path.join(d, "report.md")).read()
        ok(_declared_limits_problems(d) == [] and "does not change the recommendation; with: no opt B; without: no opt B" in rep,
           "DL8 valid test renders verdict and recommendation")
        draft("# Title\n\n## Key messages\n\n" + DECLARED_LIMITS_TOKEN + body)
        ok(_declared_limits_problems(d) == [], "DL9 any summary heading works")
        draft("# A summary of the allocation\n\n" + DECLARED_LIMITS_TOKEN + "\n\n## 1. Introduction\n\nx\n")
        ok(_declared_limits_problems(d) == [], "DL10 title with summary changes nothing")
        draft(head + "Allocation is unstable (SD-7); see SD - 002.\n" + body)
        ok(bool(_declared_limits_problems(d)), "DL11 typed ids do not count")
        draft(head + "<!-- " + DECLARED_LIMITS_TOKEN + " -->\n" + body)
        ok("0 times" in (_declared_limits_problems(d) or [""])[0], "DL12 comment token ignored")
        draft(head + "```\n" + DECLARED_LIMITS_TOKEN + "\n```\n" + body)
        ok("0 times" in (_declared_limits_problems(d) or [""])[0], "DL13 fenced token ignored")
        draft(head + DECLARED_LIMITS_TOKEN + "\n\n" + DECLARED_LIMITS_TOKEN + body)
        ok("2 times" in (_declared_limits_problems(d) or [""])[0], "DL15 two tokens flagged")
        draft(head + body + "\n" + DECLARED_LIMITS_TOKEN + "\n")
        ok("after the first body section" in (_declared_limits_problems(d) or [""])[0], "DL16 token after §1 flagged")
        wd("manuscript.md", head + DECLARED_LIMITS_TOKEN + body)
        wd("report.md", head + "Hand-typed: SD-007 and SD-002." + body)
        ok("does not contain the declared-limits block" in (_declared_limits_problems(d) or [""])[0],
           "DL17 hand-edited report flagged")
        wd("report.unrendered.md", head + DECLARED_LIMITS_TOKEN + body)
        wd("manuscript.md", head + "no token here" + body)
        wd("report.md", expand_declared_limits(d, head + DECLARED_LIMITS_TOKEN + body))
        ok(_declared_limits_problems(d) == [], "DL18 unrendered snapshot used")
        os.remove(os.path.join(d, "report.unrendered.md"))
        for opener in ("<details>\n", "<div hidden>\n", '<span style="display:none">\n'):
            draft(head + opener + DECLARED_LIMITS_TOKEN + "\n" + body)
            p_html = _declared_limits_problems(d)
            ok(p_html and "raw HTML" in p_html[0], f"DL19 {opener.strip()} before token")
        draft(head + "Fund program. " + DECLARED_LIMITS_TOKEN + " and more.\n" + body)
        ok("on its own line" in (_declared_limits_problems(d) or [""])[0], "DL20 token on own line")
        draft(head + "> " + DECLARED_LIMITS_TOKEN + "\n" + body)
        ok("on its own line" in (_declared_limits_problems(d) or [""])[0], "DL20b quoted token flagged")
        wd("scope_declaration.yaml", {"declarations": [
            {"id": "SD-<b>9</b>", "blocker_id": "x", "why_unresolved": "w",
             "claim": "Unstable <details><summary>x</summary> **bold** [link](http://e) `code` <!-- c --> end"}]})
        block = render_declared_limits_block(d)
        line = [ln for ln in block.splitlines() if ln.startswith("- ")][0]
        ok("<details>" not in line and "<!--" not in line and "<b>" not in line and "\\*\\*bold\\*\\*" in line
           and "\\[link\\]" in line and "\\`code\\`" in line and "&lt;details&gt;" in line,
           f"DL21 escaped HTML/Markdown: {line}")
        wd("scope_declaration.yaml", decl)
        draft("# 2026 allocation of the program envelope\n\n## Executive summary\n\n" + DECLARED_LIMITS_TOKEN + body)
        ok(_declared_limits_problems(d) == [], "DL22 document title is not body section")
        draft("# 2026 allocation\n\n# 1. Introduction\n\n" + DECLARED_LIMITS_TOKEN + "\n")
        ok("after the first body section" in (_declared_limits_problems(d) or [""])[0], "DL22b second H1 counts")
        wd("scope_declaration.yaml", "declared_at: 2026-13-45\ndeclarations: []\n")
        p_unr = _declared_limits_problems(d)
        ok(p_unr and "scope_declaration.yaml" in p_unr[0] and "cannot be read" in p_unr[0], "DL23 unreadable decl")
        try:
            expand_declared_limits(d, head + DECLARED_LIMITS_TOKEN + body)
            raised_dl = False
        except DeclaredLimitsError:
            raised_dl = True
        ok(raised_dl, "DL23b expand fails cleanly on unreadable declarations")

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
