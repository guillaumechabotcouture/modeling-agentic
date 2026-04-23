#!/usr/bin/env python3
"""
Spec-compliance detection and checking.

Parses a research question for named frameworks, modeling approaches,
budget envelopes, and spatial-unit counts. Checks the delivered model
code in `{run_dir}/models/` and allocation CSVs in `{run_dir}/` against
those requirements. Emits structured violations that the STAGE 7 gate
can treat as a mechanical backstop when critique agents are generous.

Design principles:

- **Conservative parsing**: only match on specific, unambiguous phrases.
  False negatives (missing a framework mention) are preferable to false
  positives that would block legitimate runs. We use positive
  indicators ("using the X framework") rather than bare mentions
  ("benchmark against X") because the latter is common in research
  questions and does not require X.

- **Pure functions**: `detect_required_spec(question)` and
  `check_spec_compliance(required, run_dir)` are deterministic and
  idempotent so the validator can call them twice and get identical
  output.

- **No external judgments**: the module does NOT decide whether the
  model is "good." It only checks mechanical compliance with explicit
  requirements parsed from the question. A spec violation means "the
  delivered work does not satisfy a stated requirement," not "the
  science is wrong."

Usage as a library:

    from scripts.spec_compliance import (
        detect_required_spec, check_spec_compliance,
    )
    required = detect_required_spec(metadata["question"])
    violations = check_spec_compliance(required, run_dir)

Usage as a CLI (self-test):

    python3 scripts/spec_compliance.py --self-test
    python3 scripts/spec_compliance.py <run_dir>  # checks the run
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
from typing import Optional


# ---------------------------------------------------------------------------
# Question parsing
# ---------------------------------------------------------------------------

# Framework positive indicators. Each pattern is paired with the framework
# name it identifies. Bare mentions like "benchmark against EMOD" are NOT
# counted as a requirement — only "using X" / "with X" / "in X" phrasing.
_FRAMEWORK_PATTERNS = [
    # starsim
    (r"\busing\s+(?:the\s+)?starsim(?:\s+framework)?", "starsim"),
    (r"\bstarsim\s+framework\b", "starsim"),
    (r"\bwith\s+starsim\b", "starsim"),
    (r"\bimplement(?:ed)?\s+in\s+starsim\b", "starsim"),
    (r"\bbuilt\s+on\s+starsim\b", "starsim"),
    # laser (typically written uppercase in epi literature)
    (r"\busing\s+(?:the\s+)?LASER(?:\s+framework)?\b", "laser"),
    (r"\bLASER\s+framework\b", "laser"),
    (r"\bbuilt\s+on\s+LASER\b", "laser"),
    # stisim
    (r"\busing\s+(?:the\s+)?stisim(?:\s+framework)?", "stisim"),
    (r"\bstisim\s+framework\b", "stisim"),
    # emod
    (r"\busing\s+(?:the\s+)?EMOD(?:\s+framework)?\b", "emod"),
    (r"\bimplement(?:ed)?\s+in\s+EMOD\b", "emod"),
]

# Approach positive indicators.
_APPROACH_PATTERNS = [
    (r"\bagent[-\s]?based\s+(?:\w+\s+){0,2}model(?:s|ling|ing)?\b", "abm"),
    (r"\bABM\b", "abm"),
    (r"\bindividual[-\s]based\s+model\b", "abm"),
    (r"\bcompartmental\b", "compartmental"),
    (r"\bstochastic\s+(?:simulation|model)\b", "stochastic"),
    (r"\bdeterministic\s+(?:\w+\s+){0,2}model\b", "deterministic"),
    (r"\b(?:linear\s+)?regression\s+model\b", "regression"),
]

# Budget envelope: match "$320M", "$1.5B", "$320 million", "~$320M".
# We avoid matching plain "320 million" without a dollar sign to reduce
# false positives on non-budget numbers.
_BUDGET_PATTERN = re.compile(
    r"\$\s?(\d+(?:\.\d+)?)\s?"
    r"(?:([MmBbKk])(?:illion)?|million|billion|thousand)",
    re.IGNORECASE,
)

_BUDGET_SUFFIX_MULT = {
    "m": 1e6, "b": 1e9, "k": 1e3,
    "million": 1e6, "billion": 1e9, "thousand": 1e3,
}

# Spatial-unit counts: "774 LGAs", "36 states", "47 counties",
# "774 Local Government Areas". Allow up to 3 intervening words so the
# number-and-unit phrase can be connected by qualifiers.
_SPATIAL_PATTERN = re.compile(
    r"\b(\d{2,6})\s+(?:\w+\s+){0,3}"
    r"(LGAs?|Local\s+Government\s+Areas?|regions?|districts?|counties|"
    r"provinces|states|wards?|sub[-\s]?counties|municipalities)\b",
    re.IGNORECASE,
)

_ARCHETYPE_PATTERN = re.compile(r"\b(\d+)\s+archetypes?\b", re.IGNORECASE)


def detect_required_spec(question: str) -> dict:
    """Parse a research question for declared requirements.

    Returns a dict with keys:
        frameworks: list[str]     — deduplicated, lower-case
        approaches: list[str]     — deduplicated, lower-case
        budget_envelope: float | None   — in dollars
        budget_raw: str | None          — the matched phrase, for display
        spatial_units: tuple[int, str] | None  — (count, unit)
        archetype_spec: int | None
    """
    frameworks: list[str] = []
    for pattern, name in _FRAMEWORK_PATTERNS:
        if re.search(pattern, question, re.IGNORECASE):
            if name not in frameworks:
                frameworks.append(name)

    approaches: list[str] = []
    for pattern, name in _APPROACH_PATTERNS:
        if re.search(pattern, question, re.IGNORECASE):
            if name not in approaches:
                approaches.append(name)

    budget_envelope: Optional[float] = None
    budget_raw: Optional[str] = None
    m = _BUDGET_PATTERN.search(question)
    if m:
        number = float(m.group(1))
        suffix = (m.group(2) or "").lower() or m.group(0).lower()
        # Map suffix to multiplier. If group(2) is None, the word form
        # (million/billion/thousand) is inside group(0); parse it.
        mult = None
        if m.group(2):
            mult = _BUDGET_SUFFIX_MULT.get(m.group(2).lower())
        else:
            for word, w_mult in _BUDGET_SUFFIX_MULT.items():
                if len(word) > 1 and word in m.group(0).lower():
                    mult = w_mult
                    break
        if mult is not None:
            budget_envelope = number * mult
            budget_raw = m.group(0)

    spatial: Optional[tuple[int, str]] = None
    m = _SPATIAL_PATTERN.search(question)
    if m:
        try:
            n = int(m.group(1))
            if n >= 10:  # sanity filter
                raw_unit = m.group(2).lower()
                # Normalize multi-word phrases to canonical short forms.
                if "local government" in raw_unit:
                    unit = "lgas"
                elif raw_unit.endswith("ies"):  # counties, municipalities
                    unit = raw_unit
                elif not raw_unit.endswith("s"):
                    unit = raw_unit + "s"
                else:
                    unit = raw_unit
                spatial = (n, unit)
        except ValueError:
            pass

    archetype_spec: Optional[int] = None
    m = _ARCHETYPE_PATTERN.search(question)
    if m:
        try:
            archetype_spec = int(m.group(1))
        except ValueError:
            pass

    return {
        "frameworks": frameworks,
        "approaches": approaches,
        "budget_envelope": budget_envelope,
        "budget_raw": budget_raw,
        "spatial_units": spatial,
        "archetype_spec": archetype_spec,
    }


# ---------------------------------------------------------------------------
# Framework/approach checks against code
# ---------------------------------------------------------------------------

def _read_models_code(run_dir: str) -> dict[str, str]:
    """Return {relative_path: source} for all .py files under run_dir/models/."""
    models_dir = os.path.join(run_dir, "models")
    if not os.path.isdir(models_dir):
        return {}
    out: dict[str, str] = {}
    for path in sorted(glob.glob(os.path.join(models_dir, "**", "*.py"),
                                 recursive=True)):
        if "__pycache__" in path:
            continue
        try:
            with open(path) as f:
                out[os.path.relpath(path, run_dir)] = f.read()
        except OSError:
            continue
    return out


# Starsim "the framework is actually running" signals. We require both an
# import AND at least one signal that a Starsim Sim is constructed or run.
# Subclassing `ss.SIS` / `ss.Disease` alone does NOT count — the malaria
# run post-mortem showed that can be done while the real dynamics are in
# scipy.integrate.odeint.
_STARSIM_IMPORT = re.compile(
    r"^\s*(?:import\s+starsim\b|from\s+starsim\s+import)", re.MULTILINE
)
_STARSIM_RUN_SIGNALS = [
    (re.compile(r"\bss\.Sim\s*\("), "ss.Sim(...) construction"),
    (re.compile(r"\bstarsim\.Sim\s*\("), "starsim.Sim(...) construction"),
    (re.compile(r"\.run\s*\(\s*\)\s*(?:#.*)?$", re.MULTILINE), "sim.run() call"),
    (re.compile(r"\bss\.People\s*\("), "ss.People(...) construction"),
    (re.compile(r"\bstarsim\.People\s*\("), "starsim.People(...) construction"),
]

# Competing ODE-integration signals that would indicate the primary
# dynamics are compartmental rather than agent-based.
_ODE_SIGNALS = [
    (re.compile(r"\bscipy\.integrate\.odeint\b|\bfrom\s+scipy\.integrate\s+import[^\n]*\bodeint\b"), "scipy.integrate.odeint"),
    (re.compile(r"\bscipy\.integrate\.solve_ivp\b|\bfrom\s+scipy\.integrate\s+import[^\n]*\bsolve_ivp\b"), "scipy.integrate.solve_ivp"),
    (re.compile(r"\bodeint\s*\("), "odeint() call"),
    (re.compile(r"\bsolve_ivp\s*\("), "solve_ivp() call"),
]

# ABM signals (per-agent state/dynamics).
_ABM_SIGNALS = [
    (re.compile(r"\bss\.People\s*\("), "ss.People(...) individual-agent container"),
    (re.compile(r"\bstarsim\.People\s*\("), "starsim.People(...) individual-agent container"),
    (re.compile(r"^class\s+\w*(?:Agent|Person|Host|Individual)\b", re.MULTILINE),
     "per-agent class definition"),
]


def _scan(patterns, code: str) -> list[str]:
    hits = []
    for pattern, label in patterns:
        if pattern.search(code):
            hits.append(label)
    return hits


def _check_framework(framework: str, code_by_file: dict[str, str]) -> Optional[dict]:
    """Return a violation dict or None if compliant."""
    if framework == "starsim":
        any_import = False
        run_signals_found: list[str] = []
        for path, code in code_by_file.items():
            if _STARSIM_IMPORT.search(code):
                any_import = True
            run_signals_found.extend(
                f"{path}: {label}" for label in _scan(_STARSIM_RUN_SIGNALS, code)
            )
        if not any_import:
            return {
                "kind": "framework_missing",
                "required": "starsim",
                "severity": "HIGH",
                "evidence": "No `import starsim` or `from starsim import` "
                            "statements found in models/",
            }
        if not run_signals_found:
            # Starsim imported but never runs. Classic cosmetic-wrap pattern.
            return {
                "kind": "framework_missing",
                "required": "starsim",
                "severity": "HIGH",
                "evidence": "Starsim is imported but never instantiates a "
                            "simulation. No `ss.Sim(...)`, `ss.People(...)`, "
                            "`starsim.Sim(...)`, or `sim.run()` calls found. "
                            "Subclassing `ss.Disease` / `ss.SIS` alone is not "
                            "sufficient — the question requires running the "
                            "simulation through Starsim's framework.",
            }
        return None  # compliant

    if framework == "laser":
        for code in code_by_file.values():
            if re.search(r"\bimport\s+laser\b|\bfrom\s+laser\b", code):
                return None
        return {
            "kind": "framework_missing",
            "required": "laser",
            "severity": "HIGH",
            "evidence": "No `import laser` or `from laser` found in models/",
        }

    if framework == "stisim":
        for code in code_by_file.values():
            if re.search(r"\bimport\s+stisim\b|\bfrom\s+stisim\b", code):
                return None
        return {
            "kind": "framework_missing",
            "required": "stisim",
            "severity": "HIGH",
            "evidence": "No `import stisim` or `from stisim` found in models/",
        }

    if framework == "emod":
        for code in code_by_file.values():
            if re.search(r"\bimport\s+emod(?:_api)?\b|\bfrom\s+emod", code):
                return None
        return {
            "kind": "framework_missing",
            "required": "emod",
            "severity": "HIGH",
            "evidence": "No `import emod` / `from emod` found in models/",
        }

    # Unknown framework — no check.
    return None


def _check_abm(code_by_file: dict[str, str]) -> Optional[dict]:
    abm_hits: list[str] = []
    ode_hits: list[str] = []
    for path, code in code_by_file.items():
        for label in _scan(_ABM_SIGNALS, code):
            abm_hits.append(f"{path}: {label}")
        for label in _scan(_ODE_SIGNALS, code):
            ode_hits.append(f"{path}: {label}")

    if abm_hits and not ode_hits:
        return None  # clearly ABM

    if not abm_hits and ode_hits:
        return {
            "kind": "approach_mismatch",
            "required": "abm",
            "severity": "HIGH",
            "evidence": (
                "No agent-based indicators found (ss.People, per-agent class, "
                "etc.). Primary dynamics appear compartmental via: "
                + "; ".join(ode_hits[:3])
                + (f" (+{len(ode_hits)-3} more)" if len(ode_hits) > 3 else "")
            ),
        }

    if abm_hits and ode_hits:
        # Both present — possible hybrid. Heuristic: if ODE signals outnumber
        # ABM signals by >2x, call it ODE-dominant.
        if len(ode_hits) > 2 * len(abm_hits):
            return {
                "kind": "approach_mismatch",
                "required": "abm",
                "severity": "HIGH",
                "evidence": (
                    f"Found ABM indicators ({len(abm_hits)}) but "
                    f"ODE/compartmental signals ({len(ode_hits)}) dominate. "
                    "Primary dynamics run through ODE solver. Examples: "
                    + "; ".join(ode_hits[:2])
                ),
            }
        return None  # mixed but ABM signals are competitive

    # Neither — no evidence either way.
    return {
        "kind": "approach_mismatch",
        "required": "abm",
        "severity": "HIGH",
        "evidence": (
            "No agent-based indicators (ss.People, per-agent class) AND no "
            "clear simulation dynamics found in models/. Cannot verify "
            "model is agent-based."
        ),
    }


# ---------------------------------------------------------------------------
# Budget check
# ---------------------------------------------------------------------------

_COST_COLUMN_CANDIDATES = ("cost", "total_cost", "budget", "price",
                           "spend", "amount", "allocated_cost")


def _find_allocation_csvs(run_dir: str) -> list[str]:
    """Return CSV paths that look like allocation outputs."""
    candidates = []
    for pattern in ("*allocation*.csv", "*budget*.csv",
                    "*optimization*.csv", "optimizer*.csv"):
        candidates.extend(glob.glob(os.path.join(run_dir, pattern)))
        candidates.extend(glob.glob(os.path.join(run_dir, "data", pattern)))
    # Deduplicate preserving order.
    seen = set()
    return [p for p in candidates if not (p in seen or seen.add(p))]


def _sum_cost_column(csv_path: str) -> tuple[Optional[float], Optional[str]]:
    """Return (sum, column_name) or (None, None) if no cost column found."""
    try:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    except (OSError, csv.Error):
        return None, None
    if not rows:
        return None, None
    cols = list(rows[0].keys())
    # Prefer exact matches over substring matches.
    cost_col = None
    for candidate in _COST_COLUMN_CANDIDATES:
        if candidate in cols:
            cost_col = candidate
            break
    if cost_col is None:
        for col in cols:
            col_l = col.lower()
            if any(c in col_l for c in ("cost", "budget", "spend")):
                # Skip per-unit costs (cost_per_X columns) — those are ratios.
                if "per_" in col_l or "_per" in col_l:
                    continue
                cost_col = col
                break
    if cost_col is None:
        return None, None

    # Optionally restrict to "allocated=True" rows if the column exists.
    alloc_filter = None
    for col in cols:
        if col.lower() == "allocated":
            alloc_filter = col
            break
    total = 0.0
    for row in rows:
        if alloc_filter is not None:
            val = row.get(alloc_filter, "").strip().lower()
            if val not in ("true", "1", "yes"):
                continue
        raw = row.get(cost_col, "").strip()
        if not raw:
            continue
        try:
            total += float(raw)
        except ValueError:
            continue
    return total, cost_col


def _check_budget(budget_envelope: float, budget_raw: str,
                  run_dir: str) -> Optional[dict]:
    csvs = _find_allocation_csvs(run_dir)
    if not csvs:
        # No allocation CSV → can't check. This is not a violation; it may
        # be that the question names a budget but no optimization step
        # produced a CSV artifact yet.
        return None

    # Pick the largest (by cost sum) allocation we find.
    best: tuple[Optional[float], Optional[str], Optional[str]] = (None, None, None)
    for path in csvs:
        total, col = _sum_cost_column(path)
        if total is not None and (best[0] is None or total > best[0]):
            best = (total, col, path)

    total, col, path = best
    if total is None:
        return None

    utilization = total / budget_envelope
    if utilization >= 0.80:
        return None  # compliant

    return {
        "kind": "budget_underutilized",
        "required": budget_raw,
        "severity": "HIGH",
        "required_budget": budget_envelope,
        "actual_spend": total,
        "utilization": utilization,
        "evidence": (
            f"sum({col}) from {os.path.basename(path)} = ${total:,.0f} "
            f"= {utilization:.1%} of {budget_raw} envelope "
            f"(${budget_envelope:,.0f}). Threshold is 80%. "
            "An optimizer that leaves >20% of the stated budget "
            "unallocated is an artifact, not a finding."
        ),
    }


# ---------------------------------------------------------------------------
# Archetype check (advisory)
# ---------------------------------------------------------------------------

def _check_archetype_aggregation(archetype_spec: int, spatial_n: Optional[int],
                                 run_dir: str) -> Optional[dict]:
    """If question names K archetypes but code uses fewer, require an
    error-bound justification in model_comparison.md or results.md."""
    # Try to find the actual archetype count used.
    # Heuristic: grep models/ for patterns like "A1", "A2", ... or for an
    # archetype list.
    models = _read_models_code(run_dir)
    archetype_ids: set[str] = set()
    arch_pattern = re.compile(r"\b(A[1-9]\d?)\b")
    for code in models.values():
        archetype_ids.update(arch_pattern.findall(code))
    k_used = len(archetype_ids) if archetype_ids else None

    if k_used is None or k_used >= archetype_spec:
        return None  # can't determine or compliant

    # Check for an error-bound statement in markdown artifacts.
    bound_keywords = ("within_archetype_error", "within-archetype error",
                      "aggregation error", "archetype variance",
                      "within-archetype PfPR variance")
    bound_found = False
    for md_name in ("model_comparison.md", "results.md",
                    "modeling_strategy.md"):
        md_path = os.path.join(run_dir, md_name)
        if not os.path.exists(md_path):
            continue
        try:
            with open(md_path) as f:
                text = f.read().lower()
            if any(kw.lower() in text for kw in bound_keywords):
                bound_found = True
                break
        except OSError:
            continue

    if bound_found:
        return None

    # Severity: HIGH if reduction is >5x, else MEDIUM.
    ratio = archetype_spec / k_used
    severity = "HIGH" if ratio >= 5 else "MEDIUM"
    return {
        "kind": "archetype_aggregation_unvalidated",
        "required": archetype_spec,
        "severity": severity,
        "used": k_used,
        "evidence": (
            f"Question names {archetype_spec} archetypes; code uses "
            f"{k_used} (grepped models/ for A1/A2/... identifiers). "
            "No within-archetype error bound or aggregation-error discussion "
            "found in model_comparison.md, results.md, or modeling_strategy.md. "
            f"{'Major (' if severity == 'HIGH' else 'Moderate ('}"
            f"{ratio:.1f}×) reduction requires a quantitative bound on "
            "within-archetype heterogeneity before LGA-level guidance is credible."
        ),
    }


# ---------------------------------------------------------------------------
# Top-level check
# ---------------------------------------------------------------------------

def check_spec_compliance(required: dict, run_dir: str) -> dict:
    """Run all applicable checks. Returns {'violations': [...]}."""
    violations: list[dict] = []
    code_by_file = _read_models_code(run_dir)

    for fw in required.get("frameworks", []):
        v = _check_framework(fw, code_by_file)
        if v is not None:
            violations.append(v)

    if "abm" in required.get("approaches", []):
        v = _check_abm(code_by_file)
        if v is not None:
            violations.append(v)

    budget = required.get("budget_envelope")
    if budget is not None:
        v = _check_budget(budget, required.get("budget_raw") or f"${budget:,.0f}",
                          run_dir)
        if v is not None:
            violations.append(v)

    arch = required.get("archetype_spec")
    if arch is not None:
        spatial_n = None
        sp = required.get("spatial_units")
        if sp is not None:
            spatial_n = sp[0]
        v = _check_archetype_aggregation(arch, spatial_n, run_dir)
        if v is not None:
            violations.append(v)

    return {"violations": violations}


# ---------------------------------------------------------------------------
# CLI / self-test
# ---------------------------------------------------------------------------

def _run_self_test() -> int:
    """Run 8 inline cases and return 0 if all pass, 1 otherwise."""
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    # --- Question parsing ---
    q1 = ("Build an agent-based model of malaria transmission across Nigeria's "
          "774 Local Government Areas (LGAs) using the Starsim framework. "
          "Benchmark against the published EMOD Nigeria analysis. "
          "Use the archetype approach from Ozodiegwu et al. 2023 (22 archetypes). "
          "Optimized cost-constrained allocation for Nigeria's Global Fund GC7 "
          "(~$320M malaria component).")
    r1 = detect_required_spec(q1)
    ok("starsim" in r1["frameworks"], "q1: starsim detected")
    ok("emod" not in r1["frameworks"],
       "q1: emod NOT required (it's a benchmark mention, not a requirement)")
    ok("abm" in r1["approaches"], "q1: abm detected")
    ok(r1["budget_envelope"] == 320e6,
       f"q1: budget=320M, got {r1['budget_envelope']}")
    ok(r1["spatial_units"] == (774, "lgas"),
       f"q1: 774 LGAs, got {r1['spatial_units']}")
    ok(r1["archetype_spec"] == 22,
       f"q1: 22 archetypes, got {r1['archetype_spec']}")

    q2 = "Fit a simple SIR model of influenza transmission."
    r2 = detect_required_spec(q2)
    ok(not r2["frameworks"], "q2: no framework required")
    ok(not r2["approaches"], "q2: no approach required")
    ok(r2["budget_envelope"] is None, "q2: no budget")

    q3 = ("Build a compartmental deterministic model of measles with "
          "$1.5 billion in interventions.")
    r3 = detect_required_spec(q3)
    ok("compartmental" in r3["approaches"], "q3: compartmental detected")
    ok("deterministic" in r3["approaches"], "q3: deterministic detected")
    ok(r3["budget_envelope"] == 1.5e9,
       f"q3: budget=$1.5B, got {r3['budget_envelope']}")

    # --- Filesystem checks ---
    with tempfile.TemporaryDirectory() as d:
        models = os.path.join(d, "models")
        os.makedirs(models)

        # Case A: question requires starsim, code has no starsim at all
        with open(os.path.join(models, "model.py"), "w") as f:
            f.write("import numpy as np\n"
                    "from scipy.integrate import odeint\n"
                    "def step(y, t): return -0.1 * y\n"
                    "odeint(step, [1.0], [0, 1, 2])\n")
        required_a = {"frameworks": ["starsim"], "approaches": ["abm"],
                      "budget_envelope": None, "budget_raw": None,
                      "spatial_units": None, "archetype_spec": None}
        va = check_spec_compliance(required_a, d)
        kinds_a = {v["kind"] for v in va["violations"]}
        ok("framework_missing" in kinds_a,
           f"A: expected framework_missing, got {kinds_a}")
        ok("approach_mismatch" in kinds_a,
           f"A: expected approach_mismatch, got {kinds_a}")

        # Case B: question requires starsim, code imports but never runs
        # (malaria-run pattern).
        with open(os.path.join(models, "model.py"), "w") as f:
            f.write("import starsim as ss\n"
                    "class Malaria(ss.SIS):\n"
                    "    pass\n"
                    "from scipy.integrate import odeint\n"
                    "odeint(lambda y, t: -0.1 * y, [1.0], [0, 1, 2])\n")
        vb = check_spec_compliance(required_a, d)
        kinds_b = {v["kind"] for v in vb["violations"]}
        ok("framework_missing" in kinds_b,
           f"B: starsim imported but not run; expected framework_missing, got {kinds_b}")

        # Case C: question requires starsim, code actually uses it.
        with open(os.path.join(models, "model.py"), "w") as f:
            f.write("import starsim as ss\n"
                    "people = ss.People(1000)\n"
                    "sim = ss.Sim(people=people, dur=10)\n"
                    "sim.run()\n")
        vc = check_spec_compliance(required_a, d)
        kinds_c = {v["kind"] for v in vc["violations"]}
        ok("framework_missing" not in kinds_c,
           f"C: starsim used properly; unexpected framework_missing in {kinds_c}")
        ok("approach_mismatch" not in kinds_c,
           f"C: ss.People present; unexpected approach_mismatch in {kinds_c}")

        # Case D: budget envelope check.
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,cost,allocated\n"
                    "A,100000000,True\n"
                    "B,41000000,True\n")
        required_d = {"frameworks": [], "approaches": [],
                      "budget_envelope": 320e6, "budget_raw": "$320M",
                      "spatial_units": None, "archetype_spec": None}
        vd = check_spec_compliance(required_d, d)
        kinds_d = {v["kind"] for v in vd["violations"]}
        ok("budget_underutilized" in kinds_d,
           f"D: $141M of $320M should trigger, got {kinds_d}")
        bu = next(v for v in vd["violations"] if v["kind"] == "budget_underutilized")
        ok(abs(bu["actual_spend"] - 141e6) < 1e3,
           f"D: actual_spend={bu['actual_spend']}, expected 141M")

        # Case E: budget is adequately utilized.
        with open(os.path.join(d, "lga_allocation.csv"), "w") as f:
            f.write("lga,cost,allocated\n"
                    "A,200000000,True\n"
                    "B,90000000,True\n")
        ve = check_spec_compliance(required_d, d)
        kinds_e = {v["kind"] for v in ve["violations"]}
        ok("budget_underutilized" not in kinds_e,
           f"E: $290M of $320M = 90.6% should NOT trigger, got {kinds_e}")

    # --- Summary ---
    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", nargs="?",
                   help="Run directory to check (requires metadata.json)")
    p.add_argument("--self-test", action="store_true",
                   help="Run inline self-test cases and exit")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON to stdout")
    args = p.parse_args()

    if args.self_test:
        return _run_self_test()

    if not args.run_dir:
        p.error("run_dir is required (or use --self-test)")

    run_dir = args.run_dir
    meta_path = os.path.join(run_dir, "metadata.json")
    if not os.path.exists(meta_path):
        print(f"ERROR: {meta_path} not found", file=sys.stderr)
        return 2
    with open(meta_path) as f:
        meta = json.load(f)
    question = meta.get("question", "")
    if not question:
        print(f"ERROR: {meta_path} has no 'question' field", file=sys.stderr)
        return 2

    required = detect_required_spec(question)
    result = check_spec_compliance(required, run_dir)

    # Human-readable to stderr.
    print(f"Spec requirements parsed from question:", file=sys.stderr)
    print(f"  frameworks: {required['frameworks']}", file=sys.stderr)
    print(f"  approaches: {required['approaches']}", file=sys.stderr)
    if required["budget_envelope"]:
        print(f"  budget: {required['budget_raw']} "
              f"(${required['budget_envelope']:,.0f})", file=sys.stderr)
    if required["spatial_units"]:
        print(f"  spatial: {required['spatial_units'][0]} "
              f"{required['spatial_units'][1]}", file=sys.stderr)
    if required["archetype_spec"]:
        print(f"  archetypes: {required['archetype_spec']}", file=sys.stderr)
    print(f"Violations: {len(result['violations'])}", file=sys.stderr)
    for v in result["violations"]:
        print(f"  [{v['severity']}] {v['kind']}: {v['evidence']}",
              file=sys.stderr)

    if args.json:
        print(json.dumps({"required": required,
                          "violations": result["violations"]}, indent=2))

    return 0 if not result["violations"] else 1


if __name__ == "__main__":
    sys.exit(main())
