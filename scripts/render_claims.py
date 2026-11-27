"""Render `[CLAIM:id]` references in reports.

Usage
-----
    python3 scripts/render_claims.py --self-test
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys

import yaml

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
    protected = code_spans(text, inline=True)

    def _sub(m: re.Match) -> str:
        if in_spans(protected, m.start()):
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

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true", required=True)
    p.parse_args()
    return _self_test()
