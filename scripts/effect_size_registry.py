#!/usr/bin/env python3
"""
Effect-size priors registry: parser, validator, sampler.

Reads the `## Parameter Registry` section of a run's `citations.md`, resolves
per-parameter `code_refs` against the repo, and detects mechanical errors:

- `registry_value_mismatch`: code literal differs from registered value by >1%.
- `or_rr_conflation`: `kind: odds_ratio` used as if it were a relative risk
  (no conversion function call near the use site).
- `cost_crosscheck_mismatch`: `kind: cost_usd` disagrees with values in a CSV
  referenced via code_refs.
- `param_unregistered`: code has `# @registry:X` comment but X not in the registry.
- `registry_missing_ref`: a code_ref points to a non-existent file:line.

Also exposes `load_priors(path)`, `resolve_code_refs(registry, repo_root)`, and
`sample_prior(entry, n, rng)` for other scripts (e.g., the Commit B
uncertainty-propagation stage).

Usage:
    python3 scripts/effect_size_registry.py <run_dir> [--repo-root .] [--json]
    python3 scripts/effect_size_registry.py --self-test

Exit codes:
    0  no violations
    1  violations found
    2  registry or file error (not a violation — can't run)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Optional

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


VALID_KINDS = {
    "odds_ratio", "relative_risk", "hazard_ratio", "incidence_rate_ratio",
    "efficacy", "coverage", "proportion", "rate", "cost_usd",
    "prevalence", "duration_days",
}
RATIO_KINDS = {"odds_ratio", "relative_risk", "hazard_ratio", "incidence_rate_ratio"}
BOUNDED_01_KINDS = {"efficacy", "coverage", "proportion", "prevalence"}
POSITIVE_KINDS = {"rate", "cost_usd", "duration_days"}

# Tokens that indicate a legitimate OR→RR conversion near the use site.
_CONVERSION_TOKENS = (
    "or_to_rr", "odds_to_risk", "odds_to_rr", "risk_from_odds", "convert_or",
    "p_baseline", "/ (1 -", "/ (1 +",
)


# ---------------------------------------------------------------------------
# Parse citations.md
# ---------------------------------------------------------------------------

_YAML_BLOCK_RE = re.compile(
    r"##\s*Parameter\s+Registry[^\n]*\n(?:.*?\n)*?"
    r"```(?:yaml|yml)\s*\n"
    r"(?P<body>.*?)"
    r"\n```",
    re.IGNORECASE | re.DOTALL,
)

# Match per-parameter detail sections like `### <name> (detail)` or bare
# `### <name>`. Captures the name from the first word after ###.
_DETAIL_HEADER_RE = re.compile(
    r"^###\s+(?P<name>[A-Za-z_][\w]*)(?:\s+\([^)]*\))?\s*$",
    re.MULTILINE,
)

# Match a top-level bullet within a detail section:
#   - **key**: value...
# Used to harvest non-code-refs fields.
_DETAIL_BULLET_RE = re.compile(
    r"^-\s+\*\*(?P<key>[\w_-]+)\*\*\s*:\s*(?P<value>.*?)(?=\n-\s+\*\*|\n###|\n##|\Z)",
    re.MULTILINE | re.DOTALL,
)

# Within a `code_refs` bullet, each sub-bullet is a file:line reference.
_CODE_REF_LINE_RE = re.compile(
    r"^\s*-\s+([^\n]+)$",
    re.MULTILINE,
)


def _parse_detail_sections(text: str, yaml_end: int) -> dict[str, dict]:
    """Scan the portion of citations.md after the YAML block for per-
    parameter detail sections. Returns {name: {field: value, ...}} with
    `code_refs` parsed from sub-bullets. Other Markdown fields are stored
    as raw strings (caller can decide which to merge).

    Detail section format (lines 0+ match `^###`):
        ### <name> (detail)        <-- or just `### <name>`
        - **name**: <name>
        - **value**: <v>
        - **kind**: <k>
        - **code_refs**:
          - models/foo.py:12 (LLIN_OR = 0.44)
          - models/bar.py:33
        - **conversion**: ...
    """
    tail = text[yaml_end:]
    out: dict[str, dict] = {}

    # Enumerate section headers with their start positions.
    headers = list(_DETAIL_HEADER_RE.finditer(tail))
    for i, hdr in enumerate(headers):
        name = hdr.group("name")
        start = hdr.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(tail)
        body = tail[start:end]

        details: dict = {}
        for bullet in _DETAIL_BULLET_RE.finditer(body):
            key = bullet.group("key").lower()
            value = bullet.group("value")
            if key == "code_refs":
                # Sub-bullets: each line `  - <ref>` becomes a cleaned ref.
                refs = []
                for m in _CODE_REF_LINE_RE.finditer(value):
                    raw = m.group(1).strip()
                    # Drop trailing parenthetical annotations, keep "file:line".
                    # `models/optimization.py:73 (LLIN_OR = 0.44)` -> `models/optimization.py:73`
                    cleaned = re.split(r"\s*\(", raw, maxsplit=1)[0].strip()
                    if cleaned:
                        refs.append(cleaned)
                if refs:
                    details["code_refs"] = refs
            else:
                # Single-line value: strip trailing whitespace/newlines.
                details[key] = value.strip()

        if details:
            out[name] = details
    return out


def load_priors(citations_md_path: str) -> dict[str, Any]:
    """Parse the `## Parameter Registry` YAML block from citations.md.

    Also scans any `### <name>` / `### <name> (detail)` sections that
    follow the YAML block and merges their fields (especially
    `code_refs`) into matching YAML entries. YAML wins on conflict —
    detail sections only fill in fields the YAML omitted.

    Returns {'parameters': [dict, ...]}. Raises on parse errors.
    Returns empty list if the section is absent.
    """
    if not os.path.exists(citations_md_path):
        raise FileNotFoundError(f"citations.md not found: {citations_md_path}")
    with open(citations_md_path) as f:
        text = f.read()
    m = _YAML_BLOCK_RE.search(text)
    if m is None:
        return {"parameters": []}
    try:
        body = yaml.safe_load(m.group("body"))
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML in Parameter Registry: {e}")
    if not isinstance(body, dict) or "parameters" not in body:
        raise ValueError("Parameter Registry YAML must be a mapping with "
                         "a top-level `parameters:` list")
    params = body.get("parameters") or []
    if not isinstance(params, list):
        raise ValueError("`parameters:` must be a list")

    # Merge detail sections from after the YAML fence.
    details_by_name = _parse_detail_sections(text, m.end())

    for i, p in enumerate(params):
        if not isinstance(p, dict):
            raise ValueError(f"parameters[{i}] must be a mapping")
        for key in ("name", "value", "kind", "source"):
            if key not in p:
                raise ValueError(f"parameters[{i}] missing required key '{key}'")
        if p["kind"] not in VALID_KINDS:
            raise ValueError(f"parameters[{i}] has invalid kind "
                             f"{p['kind']!r}; valid: {sorted(VALID_KINDS)}")
        # Fill in missing fields from the detail section if present.
        name = p["name"]
        if name in details_by_name:
            for key, value in details_by_name[name].items():
                if key not in p or p[key] in (None, "", []):
                    p[key] = value
    return body


# ---------------------------------------------------------------------------
# Code-ref resolution
# ---------------------------------------------------------------------------

@dataclass
class ResolvedRef:
    path: str                 # filesystem path
    line: Optional[int]       # line number, or None for whole-file refs (CSVs)
    exists: bool
    line_text: Optional[str]  # the referenced line's content, if applicable
    numeric_literal: Optional[float]  # extracted from the line, if found
    context: list[str]        # +/-5 lines around the use site


_NUMERIC_LITERAL_RE = re.compile(r"[-+]?(?:\d+\.\d+|\d+)(?:[eE][-+]?\d+)?")
_REGISTRY_TAG_RE = re.compile(r"#\s*@registry\s*:\s*([A-Za-z_][\w]*)")


def _resolve_one(code_ref: str, repo_root: str) -> ResolvedRef:
    # Accept "models/foo.py:123" (file:line) or "data/costs.csv" (whole-file).
    parts = code_ref.rsplit(":", 1)
    if len(parts) == 2 and parts[1].isdigit():
        rel_path, line_str = parts
        line = int(line_str)
    else:
        rel_path = code_ref
        line = None

    abs_path = os.path.join(repo_root, rel_path)
    if not os.path.exists(abs_path):
        return ResolvedRef(abs_path, line, False, None, None, [])

    if line is None:
        return ResolvedRef(abs_path, None, True, None, None, [])

    try:
        with open(abs_path) as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return ResolvedRef(abs_path, line, True, None, None, [])

    if line < 1 or line > len(lines):
        return ResolvedRef(abs_path, line, False, None, None, [])

    line_text = lines[line - 1].rstrip("\n")
    lo = max(0, line - 6)
    hi = min(len(lines), line + 5)
    context = [l.rstrip("\n") for l in lines[lo:hi]]

    # Extract numeric literal from the line. Strip the inline comment first
    # so we don't pick up numbers from the comment text. When there's an `=`
    # sign (or `:` for dict literals), prefer the FIRST numeric literal
    # after it — that's the RHS value. If no assignment punctuation, fall
    # back to first literal in the line.
    code_part = line_text.split("#", 1)[0]
    numeric_literal = None
    assign_match = re.search(r"[=:][^=]", code_part)
    search_region = code_part[assign_match.end():] if assign_match else code_part
    m = list(_NUMERIC_LITERAL_RE.finditer(search_region))
    if m:
        try:
            numeric_literal = float(m[0].group(0))
        except ValueError:
            numeric_literal = None

    return ResolvedRef(abs_path, line, True, line_text, numeric_literal, context)


def resolve_code_refs(registry: dict, repo_root: str) -> dict[str, list[ResolvedRef]]:
    """For each parameter, resolve its code_refs. Returns {name: [ResolvedRef]}."""
    out: dict[str, list[ResolvedRef]] = {}
    for p in registry.get("parameters", []):
        refs = []
        for ref in (p.get("code_refs") or []):
            refs.append(_resolve_one(ref, repo_root))
        out[p["name"]] = refs
    return out


# ---------------------------------------------------------------------------
# Sampling priors (used by Commit B)
# ---------------------------------------------------------------------------

def sample_prior(entry: dict, n: int, rng) -> list[float]:
    """Return n samples from the parameter's prior distribution.

    Accepts a `random.Random` or `numpy.random.Generator` — uses only the
    methods common to both. Kind-specific transforms:

      RATIO_KINDS: log-normal. mean(log) = log(value); sd(log) = (log(ci_high) - log(ci_low)) / (2*1.96)
      BOUNDED_01_KINDS: beta fit to (value, (ci_high - ci_low)/2)
      POSITIVE_KINDS: log-normal on untransformed scale

    If ci_low/ci_high missing, returns n copies of value (point estimate).
    """
    value = float(entry["value"])
    kind = entry["kind"]
    ci_low = entry.get("ci_low")
    ci_high = entry.get("ci_high")

    if ci_low is None or ci_high is None:
        return [value] * n

    ci_low = float(ci_low)
    ci_high = float(ci_high)

    if kind in RATIO_KINDS:
        if value <= 0 or ci_low <= 0 or ci_high <= 0:
            return [value] * n
        mu = math.log(value)
        sigma = (math.log(ci_high) - math.log(ci_low)) / (2.0 * 1.96)
        if sigma <= 0:
            return [value] * n
        # Sample log-normal.
        out = []
        for _ in range(n):
            z = _randn(rng)
            out.append(math.exp(mu + sigma * z))
        return out

    if kind in BOUNDED_01_KINDS:
        # Fit beta to (mean=value, approx sd ~= (ci_high-ci_low)/3.92).
        # Method of moments: beta(α,β) with mean=m, variance=v:
        #   α = m * ((m(1-m))/v - 1),  β = (1-m)/m * α
        m = max(1e-6, min(1 - 1e-6, value))
        sd = max(1e-6, (ci_high - ci_low) / 3.92)
        v = sd * sd
        if v >= m * (1 - m):
            v = m * (1 - m) * 0.9
        common = m * (1 - m) / v - 1
        alpha = m * common
        beta = (1 - m) * common
        out = []
        for _ in range(n):
            out.append(_beta(rng, alpha, beta))
        return out

    if kind in POSITIVE_KINDS:
        # Log-normal on untransformed scale.
        if value <= 0:
            return [value] * n
        # Treat CI as symmetric on log scale approximately.
        safe_low = max(ci_low, value * 0.01)
        safe_high = max(ci_high, safe_low * 1.001)
        mu = math.log(value)
        sigma = (math.log(safe_high) - math.log(safe_low)) / (2.0 * 1.96)
        if sigma <= 0:
            return [value] * n
        out = []
        for _ in range(n):
            z = _randn(rng)
            out.append(math.exp(mu + sigma * z))
        return out

    # Fallback: symmetric normal, truncated at zero.
    sd = (ci_high - ci_low) / 3.92
    out = []
    for _ in range(n):
        z = _randn(rng)
        out.append(max(0.0, value + sd * z))
    return out


def _randn(rng) -> float:
    """Return one N(0,1) draw. Works with random.Random or numpy RNG."""
    if hasattr(rng, "normalvariate"):
        return rng.normalvariate(0.0, 1.0)
    # numpy.random.Generator
    if hasattr(rng, "standard_normal"):
        return float(rng.standard_normal())
    # numpy.random.RandomState or .random (returns float)
    return float(rng.random())  # fallback: weak


def _beta(rng, alpha: float, beta: float) -> float:
    """Return one Beta(α, β) draw."""
    if hasattr(rng, "betavariate"):
        return rng.betavariate(alpha, beta)
    if hasattr(rng, "beta"):
        return float(rng.beta(alpha, beta))
    # Fallback: use two Gamma draws.
    x = rng.gammavariate(alpha, 1.0) if hasattr(rng, "gammavariate") else alpha
    y = rng.gammavariate(beta, 1.0) if hasattr(rng, "gammavariate") else beta
    return x / (x + y)


# ---------------------------------------------------------------------------
# Violation detection
# ---------------------------------------------------------------------------

def _line_near_has_conversion(context: list[str]) -> bool:
    joined = "\n".join(context).lower()
    return any(tok in joined for tok in _CONVERSION_TOKENS)


def _csv_values_match(csv_path: str, registered_value: float,
                      tolerance: float = 0.10) -> tuple[bool, Optional[str]]:
    """Return (matched?, note). Scan CSV for any numeric column with values
    within `tolerance` of `registered_value`. Conservative: true if ANY cell
    in the CSV is within tolerance."""
    try:
        with open(csv_path) as f:
            reader = csv.reader(f)
            for i, row in enumerate(reader):
                if i > 500:  # bound work
                    break
                for cell in row:
                    try:
                        v = float(cell)
                        if v > 0 and abs(v - registered_value) / registered_value <= tolerance:
                            return True, None
                    except (ValueError, ZeroDivisionError):
                        continue
    except (OSError, csv.Error):
        return True, f"could not read {csv_path}"
    return False, None


def check_registry(registry: dict, repo_root: str) -> dict:
    """Run all mechanical checks on a loaded registry. Returns {violations: [...]}.

    Each violation is a dict with keys:
      kind:     one of the 6 check names
      severity: HIGH | MEDIUM
      name:     parameter name
      claim:    short human-readable problem
      evidence: the raw code snippet or value
    """
    violations: list[dict] = []
    resolved = resolve_code_refs(registry, repo_root)

    for p in registry.get("parameters", []):
        name = p["name"]
        kind = p["kind"]
        value = float(p["value"])
        refs = resolved.get(name, [])

        py_refs = [r for r in refs if r.exists and r.line is not None]
        csv_refs = [r for r in refs if r.exists and r.line is None]

        # 1. registry_missing_ref — any code_ref that doesn't exist on disk.
        for r in refs:
            if not r.exists:
                violations.append({
                    "kind": "registry_missing_ref",
                    "severity": "MEDIUM",
                    "name": name,
                    "claim": f"code_ref {os.path.basename(r.path)}"
                             + (f":{r.line}" if r.line else "")
                             + " does not exist",
                    "evidence": r.path,
                })

        # 2. registry_value_mismatch — code literal ≠ registry value.
        for r in py_refs:
            if r.numeric_literal is None:
                continue
            if value == 0:
                same = r.numeric_literal == 0
            else:
                same = abs(r.numeric_literal - value) / abs(value) <= 0.01
            if not same:
                violations.append({
                    "kind": "registry_value_mismatch",
                    "severity": "HIGH",
                    "name": name,
                    "claim": f"code at {os.path.basename(r.path)}:{r.line} "
                             f"has literal {r.numeric_literal} but registry "
                             f"value is {value}",
                    "evidence": r.line_text or "",
                })

        # 3. or_rr_conflation — kind=odds_ratio and no conversion nearby.
        if kind == "odds_ratio":
            for r in py_refs:
                if not _line_near_has_conversion(r.context):
                    violations.append({
                        "kind": "or_rr_conflation",
                        "severity": "HIGH",
                        "name": name,
                        "claim": (f"{name} has kind=odds_ratio but the use at "
                                  f"{os.path.basename(r.path)}:{r.line} has no "
                                  f"conversion (or_to_rr, odds_to_risk, or explicit "
                                  f"formula) in ±5 lines of context"),
                        "evidence": r.line_text or "",
                    })

        # 4. cost_crosscheck_mismatch — cost_usd vs referenced CSV.
        if kind == "cost_usd" and csv_refs:
            for cr in csv_refs:
                matched, note = _csv_values_match(cr.path, value, tolerance=0.10)
                if not matched and note is None:
                    violations.append({
                        "kind": "cost_crosscheck_mismatch",
                        "severity": "HIGH",
                        "name": name,
                        "claim": (f"{name} registry value ${value} not found "
                                  f"within 10% tolerance in {os.path.basename(cr.path)}"),
                        "evidence": cr.path,
                    })

    # 5. param_unregistered — code has @registry:X but X not in registry.
    # Only scan model code directories, not pipeline infrastructure.
    registered_names = {p["name"] for p in registry.get("parameters", [])}
    scan_roots = []
    for candidate in ("models", "src", "code"):
        full = os.path.join(repo_root, candidate)
        if os.path.isdir(full):
            scan_roots.append(full)
    # If repo_root IS the run directory itself (has a models/ subdir), we've
    # added it above. If not (e.g., caller passed the parent repo root), also
    # search any run-dir-looking subdirs.
    if not scan_roots:
        # As a fallback, scan the repo_root itself but still skip infrastructure.
        scan_roots = [repo_root]

    _pipeline_skip = ("agents", "scripts", ".claude", ".git", "__pycache__",
                      ".venv", "node_modules", "experiments", ".pytest_cache")
    for start in scan_roots:
        for root, dirs, files in os.walk(start):
            dirs[:] = [d for d in dirs if d not in _pipeline_skip]
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(root, fname)
                try:
                    with open(path) as f:
                        for lineno, line in enumerate(f, start=1):
                            m = _REGISTRY_TAG_RE.search(line)
                            if m and m.group(1) not in registered_names:
                                rel = os.path.relpath(path, repo_root)
                                violations.append({
                                    "kind": "param_unregistered",
                                    "severity": "MEDIUM",
                                    "name": m.group(1),
                                    "claim": f"{rel}:{lineno} tags @registry:"
                                             f"{m.group(1)} but no matching entry "
                                             f"in citations.md Parameter Registry",
                                    "evidence": line.strip(),
                                })
                except (OSError, UnicodeDecodeError):
                    continue

    return {"violations": violations}


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _run_self_test() -> int:
    import tempfile
    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    # --- parse ---
    with tempfile.TemporaryDirectory() as repo:
        # Minimal repo layout.
        os.makedirs(os.path.join(repo, "models"))
        os.makedirs(os.path.join(repo, "data"))
        citations_path = os.path.join(repo, "citations.md")

        # Case A: OR/RR conflation — kind=odds_ratio used without conversion.
        with open(os.path.join(repo, "models", "m.py"), "w") as f:
            f.write("# sample code\n"
                    "# @registry:irs_or\n"
                    "irs_or = 0.35\n"
                    "new_rate = old_rate * irs_or   # WRONG: treating OR as RR\n"
                    "# @registry:itn_rr\n"
                    "itn_rr = 0.55\n"
                    "new_rate = old_rate * itn_rr   # OK: RR is multiplicative\n")

        with open(citations_path, "w") as f:
            f.write("# Citations\n\n## [C1] Source\n\n## Parameter Registry\n\n"
                    "```yaml\nparameters:\n"
                    "  - name: irs_or\n"
                    "    value: 0.35\n"
                    "    ci_low: 0.27\n"
                    "    ci_high: 0.44\n"
                    "    kind: odds_ratio\n"
                    "    source: C1\n"
                    "    applies_to: IRS effect\n"
                    "    code_refs: ['models/m.py:3']\n"
                    "  - name: itn_rr\n"
                    "    value: 0.55\n"
                    "    ci_low: 0.48\n"
                    "    ci_high: 0.64\n"
                    "    kind: relative_risk\n"
                    "    source: C1\n"
                    "    applies_to: ITN effect\n"
                    "    code_refs: ['models/m.py:6']\n"
                    "```\n")

        registry = load_priors(citations_path)
        ok(len(registry["parameters"]) == 2, "A: parsed 2 params")

        result = check_registry(registry, repo)
        kinds = [v["kind"] for v in result["violations"]]
        ok("or_rr_conflation" in kinds,
           f"A: OR/RR conflation detected; got {kinds}")
        ok(not any(v["kind"] == "or_rr_conflation" and v["name"] == "itn_rr"
                   for v in result["violations"]),
           "A: no conflation flagged on itn_rr (it's a real RR)")

        # Case B: value mismatch.
        with open(os.path.join(repo, "models", "m.py"), "w") as f:
            f.write("# @registry:irs_or\n"
                    "irs_or = 0.40   # code says 0.40, registry says 0.35\n"
                    "convert = or_to_rr(irs_or, 0.4)  # add conversion\n")
        with open(citations_path, "w") as f:
            f.write("## Parameter Registry\n\n```yaml\nparameters:\n"
                    "  - name: irs_or\n"
                    "    value: 0.35\n"
                    "    kind: odds_ratio\n"
                    "    source: C1\n"
                    "    applies_to: IRS\n"
                    "    code_refs: ['models/m.py:2']\n"
                    "```\n")
        registry = load_priors(citations_path)
        result = check_registry(registry, repo)
        kinds = {v["kind"] for v in result["violations"]}
        ok("registry_value_mismatch" in kinds,
           f"B: value mismatch detected; got {kinds}")
        ok("or_rr_conflation" not in kinds,
           f"B: conversion present → no conflation (got {kinds})")

        # Case C: cost crosscheck — code $2.50/net, CSV has $10.
        with open(os.path.join(repo, "data", "costs.csv"), "w") as f:
            f.write("item,low,mid,high\nitn_pbo,8.50,10.00,12.50\n"
                    "irs,4.00,5.00,7.00\n")
        with open(os.path.join(repo, "models", "m.py"), "w") as f:
            f.write("# @registry:itn_pbo_cost\n"
                    "itn_pbo_unit = 2.50\n")
        with open(citations_path, "w") as f:
            f.write("## Parameter Registry\n\n```yaml\nparameters:\n"
                    "  - name: itn_pbo_cost\n"
                    "    value: 2.50\n"
                    "    kind: cost_usd\n"
                    "    source: C1\n"
                    "    applies_to: PBO net unit cost\n"
                    "    code_refs: ['models/m.py:2', 'data/costs.csv']\n"
                    "```\n")
        registry = load_priors(citations_path)
        result = check_registry(registry, repo)
        kinds = {v["kind"] for v in result["violations"]}
        ok("cost_crosscheck_mismatch" in kinds,
           f"C: cost mismatch detected; got {kinds}")

        # Case D: param_unregistered.
        with open(os.path.join(repo, "models", "m.py"), "w") as f:
            f.write("# @registry:irs_or\n"
                    "irs_or = 0.35\n"
                    "or_to_rr(irs_or, 0.4)\n"
                    "# @registry:orphan_param\n"
                    "orphan = 99\n")
        with open(citations_path, "w") as f:
            f.write("## Parameter Registry\n\n```yaml\nparameters:\n"
                    "  - name: irs_or\n"
                    "    value: 0.35\n"
                    "    kind: odds_ratio\n"
                    "    source: C1\n"
                    "    applies_to: IRS\n"
                    "    code_refs: ['models/m.py:2']\n"
                    "```\n")
        registry = load_priors(citations_path)
        result = check_registry(registry, repo)
        kinds = {v["kind"] for v in result["violations"]}
        ok("param_unregistered" in kinds,
           f"D: orphan @registry tag detected; got {kinds}")

        # Case E: sample_prior smoke test.
        import random
        rng = random.Random(42)
        entry_or = {"name": "x", "value": 0.35, "ci_low": 0.27, "ci_high": 0.44,
                    "kind": "odds_ratio", "source": "C1"}
        samples = sample_prior(entry_or, 100, rng)
        mean = sum(samples) / len(samples)
        ok(0.25 < mean < 0.50, f"E: log-normal mean ~ 0.35, got {mean:.3f}")
        ok(min(samples) > 0, "E: log-normal samples all positive")

        entry_eff = {"name": "y", "value": 0.55, "ci_low": 0.48, "ci_high": 0.64,
                     "kind": "efficacy", "source": "C1"}
        samples = sample_prior(entry_eff, 100, rng)
        ok(all(0 <= s <= 1 for s in samples), "E: efficacy samples in [0,1]")
        mean = sum(samples) / len(samples)
        ok(0.45 < mean < 0.65, f"E: beta mean ~ 0.55, got {mean:.3f}")

        # --- Case F: detail-section merge (Phase 3 Commit A1) ---
        # YAML registry with no code_refs; detail section below provides them.
        # Expected: after load_priors, each parameter has code_refs populated.
        citations_with_details = """# Citations

## [C1] Foo et al.

## Parameter Registry

```yaml
parameters:
  - name: foo_or
    value: 0.44
    kind: odds_ratio
    source: C1
  - name: bar_rr
    value: 0.27
    kind: relative_risk
    source: C1
```

### foo_or (detail)
- **name**: foo_or
- **value**: 0.44
- **kind**: odds_ratio
- **source**: [C1] Foo et al.
- **subgroup**: overall
- **applies_to**: FOI reduction from ITN
- **code_refs**:
  - models/optimization.py:73 (FOO_OR = 0.44)
  - models/outcome_fn.py:46 (foo_or default)
- **conversion**: OR-to-RR via or_to_rr

### bar_rr
- **name**: bar_rr
- **value**: 0.27
- **kind**: relative_risk
- **source**: [C1] Foo et al.
- **code_refs**:
  - models/optimization.py:85
"""
        citations_path2 = os.path.join(repo, "citations_details.md")
        with open(citations_path2, "w") as f:
            f.write(citations_with_details)
        merged = load_priors(citations_path2)
        params_by_name = {p["name"]: p for p in merged["parameters"]}
        ok("foo_or" in params_by_name, "F: foo_or parsed")
        ok(params_by_name["foo_or"].get("code_refs") == [
            "models/optimization.py:73", "models/outcome_fn.py:46"],
           f"F: foo_or code_refs got {params_by_name['foo_or'].get('code_refs')}")
        ok(params_by_name["foo_or"].get("subgroup") == "overall",
           "F: foo_or subgroup merged from detail")
        ok(params_by_name["bar_rr"].get("code_refs") == [
            "models/optimization.py:85"],
           f"F: bar_rr code_refs got {params_by_name['bar_rr'].get('code_refs')}")

        # --- Case G: YAML-provided code_refs WIN over detail section ---
        # (Detail section should only fill in what's missing.)
        citations_yaml_wins = """## Parameter Registry

```yaml
parameters:
  - name: baz_or
    value: 0.5
    kind: odds_ratio
    source: C1
    code_refs: ['models/yaml_source.py:10']
```

### baz_or (detail)
- **code_refs**:
  - models/detail_source.py:99
"""
        citations_path3 = os.path.join(repo, "citations_yaml_wins.md")
        with open(citations_path3, "w") as f:
            f.write(citations_yaml_wins)
        merged2 = load_priors(citations_path3)
        baz = next(p for p in merged2["parameters"] if p["name"] == "baz_or")
        ok(baz["code_refs"] == ["models/yaml_source.py:10"],
           f"G: YAML code_refs wins; got {baz['code_refs']}")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", nargs="?",
                   help="Run directory containing citations.md")
    p.add_argument("--repo-root", default=None,
                   help="Repo root for resolving code_refs (defaults to "
                        "current working directory)")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON to stdout")
    p.add_argument("--self-test", action="store_true",
                   help="Run inline self-tests and exit")
    args = p.parse_args()

    if args.self_test:
        return _run_self_test()

    if not args.run_dir:
        p.error("run_dir is required (or use --self-test)")

    citations_path = os.path.join(args.run_dir, "citations.md")
    if not os.path.exists(citations_path):
        print(f"ERROR: {citations_path} not found", file=sys.stderr)
        return 2

    repo_root = args.repo_root or os.getcwd()

    try:
        registry = load_priors(citations_path)
    except (ValueError, FileNotFoundError) as e:
        print(f"ERROR loading registry: {e}", file=sys.stderr)
        return 2

    result = check_registry(registry, repo_root)
    violations = result["violations"]

    print(f"Parameter registry: {len(registry.get('parameters', []))} entries",
          file=sys.stderr)
    print(f"Violations: {len(violations)}", file=sys.stderr)
    for v in violations:
        print(f"  [{v['severity']}] {v['kind']} ({v['name']}): {v['claim']}",
              file=sys.stderr)

    if args.json:
        print(json.dumps(result, indent=2))

    return 0 if not violations else 1


if __name__ == "__main__":
    sys.exit(main())
