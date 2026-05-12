"""Phase 18 α — Render `[CLAIM:id]` references in report.md.

The writer drafts `report.md` using `[CLAIM:claim_id]` references for
every numeric / percentage / currency / count / verdict-label / citation
claim. This script reads `models/claims_ledger.yaml` (produced by the
analyst), looks up each referenced claim, formats it, and substitutes
the rendered string back into `report.md`. Free prose is preserved.

Why this exists
---------------
Phase 17 retro on 2026-04-29_161312 had multiple drift classes that
survived to ACCEPT (R-019 verdict mismatch, R-020 LGA count drift,
$197M cost conflation). The coherence auditor catches drift after
the fact; the claims ledger PREVENTS drift structurally — the writer
cannot re-narrate a number that doesn't exist in the ledger.

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
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from typing import Any

import yaml


_CLAIM_REF_RE = re.compile(r"\[CLAIM:([A-Za-z0-9_]+)\]")

# ----- Magnitude helpers ------------------------------------------------ #


def _format_magnitude(v: float, decimals: int = 2) -> str:
    """Render a number with M/K suffix appropriate to magnitude.

    >>> _format_magnitude(7467997)
    '7.47M'
    >>> _format_magnitude(320_000_000)
    '320M'
    >>> _format_magnitude(2_300_000_000)
    '2.30B'
    >>> _format_magnitude(63)
    '63'
    """
    av = abs(v)
    if av >= 1_000_000_000:
        return f"{v / 1_000_000_000:.{decimals}f}".rstrip("0").rstrip(".") + "B"
    if av >= 1_000_000:
        return f"{v / 1_000_000:.{decimals}f}".rstrip("0").rstrip(".") + "M"
    if av >= 1_000:
        return f"{v / 1_000:.{decimals}f}".rstrip("0").rstrip(".") + "K"
    if isinstance(v, float) and not v.is_integer():
        return f"{v:.{decimals}f}".rstrip("0").rstrip(".")
    return str(int(v))


def _format_currency(v: float, decimals: int = 1) -> str:
    """Render a currency value with $ prefix and magnitude suffix."""
    if abs(v) >= 1_000_000_000:
        return f"${v / 1_000_000_000:.{decimals}f}".rstrip("0").rstrip(".") + "B"
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.{decimals}f}".rstrip("0").rstrip(".") + "M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"


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

    if kind == "scalar":
        body = _format_magnitude(float(val))
        if has_ci:
            body += (f" (95% CI: {_format_magnitude(float(ci_low))}"
                     f"-{_format_magnitude(float(ci_high))})")
        return body

    if kind == "count":
        return str(int(val))

    if kind == "percentage":
        body = f"{float(val):.1f}%".replace(".0%", "%")
        if has_ci:
            lo = f"{float(ci_low):.1f}%".replace(".0%", "%")
            hi = f"{float(ci_high):.1f}%".replace(".0%", "%")
            body += f" (95% CI: {lo}-{hi})"
        return body

    if kind == "currency":
        body = _format_currency(float(val))
        if has_ci:
            body += (f" (95% CI: {_format_currency(float(ci_low))}"
                     f"-{_format_currency(float(ci_high))})")
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
        if cid in out:
            raise ValueError(f"{ledger_path}: duplicate claim id {cid!r}")
        kind = c.get("claim_kind")
        if kind not in _VALID_KINDS:
            raise ValueError(
                f"{ledger_path}: claims[{i}] kind {kind!r} not in "
                f"{sorted(_VALID_KINDS)}")
        # CI consistency
        if kind in ("scalar", "percentage"):
            v = c.get("value")
            lo = c.get("ci_low")
            hi = c.get("ci_high")
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
    """
    unresolved: list[str] = []

    def _sub(m: re.Match) -> str:
        cid = m.group(1)
        c = claims.get(cid)
        if c is None:
            unresolved.append(cid)
            return m.group(0)  # leave reference intact
        return render_claim(c)

    new_text = _CLAIM_REF_RE.sub(_sub, text)
    return new_text, unresolved


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
    ledger_path = os.path.join(run_dir, "models", "claims_ledger.yaml")
    if not os.path.exists(report_path):
        return {"ok": False, "error": f"report.md not found at {report_path}",
                "n_refs": 0, "n_unresolved": 0, "unresolved_ids": []}
    if not os.path.exists(ledger_path):
        return {"ok": False,
                "error": f"claims_ledger.yaml not found at {ledger_path}",
                "n_refs": 0, "n_unresolved": 0, "unresolved_ids": []}

    with open(report_path) as f:
        text = f.read()

    n_refs = len(_CLAIM_REF_RE.findall(text))
    if n_refs == 0:
        # No references; nothing to do
        return {"ok": True, "n_refs": 0, "n_unresolved": 0,
                "unresolved_ids": [], "snapshot_path": None,
                "report_path": report_path}

    claims = load_claims(ledger_path)

    # Snapshot the writer's draft before substitution.
    snapshot_path = os.path.join(run_dir, "report.unrendered.md")
    shutil.copy2(report_path, snapshot_path)

    new_text, unresolved = render_text(text, claims)

    if unresolved:
        # Partial render still written so the lead can see what landed.
        # Caller (CLI / agent) can decide whether to halt or escalate.
        with open(report_path, "w") as f:
            f.write(new_text)
        return {"ok": False, "n_refs": n_refs,
                "n_unresolved": len(set(unresolved)),
                "unresolved_ids": sorted(set(unresolved)),
                "snapshot_path": snapshot_path,
                "report_path": report_path}

    with open(report_path, "w") as f:
        f.write(new_text)
    return {"ok": True, "n_refs": n_refs, "n_unresolved": 0,
            "unresolved_ids": [], "snapshot_path": snapshot_path,
            "report_path": report_path}


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
    if result.get("unresolved_ids"):
        print(f"unresolved IDs:", file=sys.stderr)
        for cid in result["unresolved_ids"]:
            print(f"  - {cid}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
