#!/usr/bin/env python3
"""Plant known defects into CRITIQUE-stage fixtures (experiment E5).

    python3 scripts/plant_defects.py build --fixtures <critique_fixtures_root> --out <root>
                                     [--k 3] [--per-variant 3] [--seed 7] [--only F ...]
    python3 scripts/plant_defects.py list --out <root>
    python3 scripts/plant_defects.py --self-test

For each source fixture F it writes F__clean (unchanged) and F__p1..F__pk,
each with `per-variant` non-overlapping planted defects (distinct files).
Answer keys go to <root>/_truth/<variant>.yaml, OUTSIDE every fixture dir, so
nothing a replayed critic can read (inputs/ is the only thing copied into its
workspace) contains the planted-defect list.

Each defect class is a disease-agnostic function: it finds a plausible target
in a COPY of the inputs, edits it in place with a minimal textual change (no
YAML round-trips that would reformat the file), and returns a manifest entry:

    defect_id, class, file, location, severity (decision-critical|major|minor),
    description, terms: {specific: [...], generic: [...]}

`specific` terms are strings only this defect produces (the changed value, the
column, the claim id); `generic` terms are the vocabulary a correct critique
would use. scripts/score_critiques.py matches a blocker to a defect when the
blocker cites the defect's file or location AND contains >=1 specific term or
>=2 generic terms.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import random
import re
import shutil
import sys
import tempfile

import yaml

NUM = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"


def _read(p):
    return open(p, encoding="utf-8", errors="replace").read()


def _write(p, s):
    open(p, "w", encoding="utf-8").write(s)


def _rel(root, p):
    return os.path.relpath(p, root)


def _first(root, patterns):
    for pat in patterns:
        hits = sorted(glob.glob(os.path.join(root, pat), recursive=True))
        if hits:
            return hits
    return []


def _fmt_like(old: str, new: float) -> str:
    """Format `new` like the numeric literal `old` (int vs float, decimals)."""
    if re.fullmatch(r"-?\d+", old):
        return str(int(round(new)))
    if "e" in old.lower():
        return f"{new:.6e}"
    dec = len(old.split(".")[1]) if "." in old else 0
    return f"{new:.{max(dec, 1)}f}"


def _block(lines, start, indent):
    """Line indices of the YAML block starting at `start` (deeper indentation)."""
    out = []
    for j in range(start + 1, len(lines)):
        l = lines[j]
        if l.strip() and (len(l) - len(l.lstrip())) <= indent:
            break
        out.append(j)
    return out


def _uq_outputs(path):
    """(name, line_index, indent, {field: (line, value_str)}) for every output
    block with numeric mean/ci_low/ci_high in an uncertainty report."""
    lines = _read(path).split("\n")
    res = []
    for i, l in enumerate(lines):
        m = re.match(r"^(\s*)([\w.\-]+):\s*$", l)
        if not m:
            continue
        ind = len(m.group(1))
        fields = {}
        for j in _block(lines, i, ind):
            mm = re.match(r"^\s+(mean|median|ci_low|ci_high):\s*(" + NUM + r")\s*$", lines[j])
            if mm and (len(lines[j]) - len(lines[j].lstrip())) > ind:
                fields.setdefault(mm.group(1), (j, mm.group(2)))
        if {"mean", "ci_low", "ci_high"} <= set(fields):
            res.append((m.group(2), i, ind, fields))
    return lines, res


# --------------------------------------------------------------------------- defect classes

def plant_zero_width_ci(root, rng, used):
    for p in _first(root, ["uncertainty_report.yaml", "models/uncertainty_report.yaml"]):
        lines, outs = _uq_outputs(p)
        ok = [o for o in outs if float(o[3]["ci_low"][1]) < float(o[3]["ci_high"][1]) and float(o[3]["mean"][1]) != 0]
        if not ok:
            continue
        name, _, _, f = rng.choice(ok)
        mean_s = f["mean"][1]
        for k in ("ci_low", "ci_high"):
            j, old = f[k]
            lines[j] = lines[j].replace(old, mean_s, 1)
        _write(p, "\n".join(lines))
        return {"class": "zero_width_ci", "file": _rel(root, p), "location": f"{name}.ci_low/ci_high",
                "severity": "decision-critical",
                "description": f"95% interval for `{name}` collapsed to the point estimate ({mean_s}) although draws vary.",
                "terms": {"specific": [name, "zero-width", "zero width"],
                          "generic": ["degenerate", "identical", "collapsed", "no uncertainty", "ci_low",
                                      "ci_high", "interval", "point estimate", "same value"]}}
    return None


def plant_ci_inverted(root, rng, used):
    for p in _first(root, ["uncertainty_report.yaml", "models/uncertainty_report.yaml"]):
        lines, outs = _uq_outputs(p)
        ok = [o for o in outs if float(o[3]["ci_low"][1]) < float(o[3]["mean"][1]) < float(o[3]["ci_high"][1])
              and o[0] not in used.get("uq_names", set())]
        if not ok:
            continue
        name, _, _, f = rng.choice(ok)
        hi = float(f["ci_high"][1])
        j, old = f["ci_low"]
        new = _fmt_like(old, hi * 1.15 if hi > 0 else hi * 0.85)
        lines[j] = lines[j].replace(old, new, 1)
        _write(p, "\n".join(lines))
        return {"class": "ci_inverted", "file": _rel(root, p), "location": f"{name}.ci_low",
                "severity": "major",
                "description": f"`{name}` ci_low set to {new}, above both the mean and ci_high (inverted interval).",
                "terms": {"specific": [name, new], "generic": ["inverted", "lower bound", "exceeds", "above the mean",
                                                             "greater than", "ci_low", "inconsistent", "interval"]}}
    return None


def plant_ledger_mismatch(root, rng, used):
    for p in _first(root, ["models/claims_ledger.yaml", "claims_ledger.yaml"]):
        lines = _read(p).split("\n")
        cands = []
        for i, l in enumerate(lines):
            m = re.match(r"^(\s*)- id:\s*['\"]?([\w.\-]+)['\"]?\s*$", l)
            if not m:
                continue
            blk = _block(lines, i, len(m.group(1)))
            txt = "\n".join(lines[j] for j in blk)
            vj = next((j for j in blk if re.match(r"^\s+value:\s*" + NUM + r"\s*$", lines[j])), None)
            if vj is None or not re.search(r"source_artifact:\s*\S+\.(ya?ml|csv|json)", txt):
                continue
            val = re.search(NUM + r"\s*$", lines[vj]).group(0).strip()
            if float(val) == 0 or abs(float(val)) < 2:
                continue
            cands.append((m.group(2), vj, val))
        if not cands:
            continue
        cid, vj, old = rng.choice(cands)
        new = _fmt_like(old, float(old) * 1.37)
        lines[vj] = re.sub(NUM + r"(\s*)$", new + r"\1", lines[vj])
        _write(p, "\n".join(lines))
        src_art = re.search(r"source_artifact:\s*(\S+)", "\n".join(lines[vj - 8:vj + 8]))
        return {"class": "ledger_mismatch", "file": _rel(root, p), "location": f"claims[{cid}].value",
                "also_files": [src_art.group(1).strip("'\"")] if src_art else [],
                "severity": "major",
                "description": f"Ledger claim `{cid}` value changed {old} -> {new}; its source artifact still says {old}.",
                "terms": {"specific": [cid, new], "generic": ["ledger", "source", "mismatch", "does not match",
                                                            "inconsistent", "discrepan", "differs"]}}
    return None


_PROSE_NUM = re.compile(r"(?<![\w.$])(\d{1,3}(?:,\d{3})+|\d{4,})(?:\.\d+)?(?![\w%])")


def plant_prose_stale_number(root, rng, used):
    for p in _first(root, ["results.md"]):
        lines = _read(p).split("\n")
        cands = []
        for i, l in enumerate(lines):
            if l.lstrip().startswith(("|", "#", "```")) or len(l) < 40:
                continue
            for m in _PROSE_NUM.finditer(l):
                raw = m.group(1).replace(",", "")
                if 1900 <= int(raw) <= 2100:          # years
                    continue
                if int(raw) >= 1000:
                    cands.append((i, m.group(1), m.start(1)))
        if not cands:
            continue
        i, old, pos = rng.choice(cands)
        newv = int(int(old.replace(",", "")) * 0.82)
        new = f"{newv:,}" if "," in old else str(newv)
        lines[i] = lines[i][:pos] + new + lines[i][pos + len(old):]
        _write(p, "\n".join(lines))
        return {"class": "prose_stale_number", "file": _rel(root, p), "location": f"line {i + 1}",
                "severity": "major",
                "description": f"results.md line {i + 1}: {old} changed to {new}; artifacts still support {old}.",
                "terms": {"specific": [new, new.replace(",", "")],
                          "generic": ["results.md", "stale", "inconsistent", "does not match", "discrepan",
                                      "mismatch", "differs", "contradict"]}}
    return None


_NOT_MODEL = re.compile(r"(verify|check|test|figure|plot|eda|patch|debug|diag|scratch|explore)", re.I)
_ASSIGN = re.compile(r"^\s*([A-Za-z_][\w\[\]'\".]*)\s*[+\-*/]?=(?!=)")


def _model_code(root):
    return [p for p in _first(root, ["models/**/*.py"]) + _first(root, ["*.py"])
            if not _NOT_MODEL.search(os.path.basename(p))]


ELIG = ["pop_u5", "u5_pop", "population_u5", "pop_under5", "under5_pop", "n_u5", "u5", "eligible_pop",
        "pop_eligible", "n_eligible", "eligible", "target_pop", "pop_target", "children", "pop_children"]
TOTAL = ["total_population", "total_pop", "pop_total", "population", "pop"]


def plant_benefit_total_population(root, rng, used):
    """Swap the eligible-population identifier for the total-population one in an
    assignment whose LHS is a benefit quantity (cases/dalys/deaths averted...).
    Identifiers inside string literals and non-model scripts are never touched."""
    cands = []
    for p in _model_code(root):
        src = _read(p)
        tot = next((t for t in TOTAL if re.search(r"(?<![\"'\w])" + re.escape(t) + r"(?![\"'\w])", src)), None)
        if not tot:
            continue
        for i, l in enumerate(src.split("\n")):
            m = _ASSIGN.match(l)
            if not m or "*" not in l or not re.search(r"(averted|benefit|cases|dalys|deaths|impact)", m.group(1), re.I):
                continue
            rhs = l[m.end():]
            for e in ELIG:
                if e != tot and re.search(r"(?<![\"'\w])" + re.escape(e) + r"(?![\"'\w])", rhs):
                    cands.append((p, i, e, tot))
                    break
    if not cands:
        return None
    p, i, e, tot = rng.choice(cands)
    lines = _read(p).split("\n")
    m = _ASSIGN.match(lines[i])
    lines[i] = lines[i][:m.end()] + re.sub(r"(?<![\"'\w])" + re.escape(e) + r"(?![\"'\w])", tot,
                                         lines[i][m.end():], count=1)
    _write(p, "\n".join(lines))
    return {"class": "benefit_total_population", "file": _rel(root, p), "location": f"line {i + 1}",
            "severity": "decision-critical",
            "description": f"{_rel(root, p)}:{i + 1}: benefit computed on `{tot}` instead of the eligible `{e}`.",
            "terms": {"specific": [f"{os.path.basename(p)}:{i + 1}", tot, e],
                      "generic": ["eligible", "denominator", "total population", "under-5", "under five",
                                  "target population", "overestimat", "inflat", "whole population"]}}


KNOWN_VERDICTS = {"CLEAN", "DEGENERATE_FIT_DETECTED", "INSUFFICIENT_STRUCTURES", "UNIDENTIFIED_PARAMETERS",
                  "WEAKLY_IDENTIFIED_PARAMETERS", "UNKNOWN_LOSS_SCALE"}


def plant_renamed_verdict(root, rng, used):
    for p in _first(root, ["model_comparison_formal.yaml", "identifiability.yaml",
                           "models/model_comparison_formal.yaml"]):
        s = _read(p)
        m = re.search(r"^verdict:\s*['\"]?([A-Z_]+)['\"]?\s*$", s, re.M)
        if not m or m.group(1) not in KNOWN_VERDICTS:
            continue
        new = "VALIDATED_WITH_NOTES"
        _write(p, s[:m.start(1)] + new + s[m.end(1):])
        return {"class": "renamed_verdict", "file": _rel(root, p), "location": "verdict",
                "severity": "major",
                "description": f"{_rel(root, p)} verdict `{m.group(1)}` replaced by the invented `{new}`.",
                "terms": {"specific": [new], "generic": ["verdict", "invalid", "not a valid", "renamed",
                                                         "unrecognized", "unknown", "invented", "edited"]}}
    return None


COST_COLS = ("cost", "cost_usd", "total_cost", "annual_cost", "cost_3yr", "cost_usd_3yr", "budget_usd")


def plant_budget_overspend(root, rng, used):
    csvs = [p for p in _first(root, ["**/*allocation*.csv"]) if "uniform" not in p and "baseline" not in p]
    csvs.sort(key=lambda p: (0 if "optim" in p else 1, -os.path.getsize(p)))
    for p in csvs:
        lines = _read(p).split("\n")
        hdr = lines[0].split(",")
        ci = next((i for i, h in enumerate(hdr) if h.strip().lower() in COST_COLS), None)
        if ci is None:
            continue
        changed = 0
        for j in range(1, len(lines)):
            cells = lines[j].split(",")
            if len(cells) != len(hdr):
                continue
            try:
                v = float(cells[ci])
            except ValueError:
                continue
            if v > 0:
                cells[ci] = _fmt_like(cells[ci], v * 1.55)
                lines[j] = ",".join(cells); changed += 1
        if not changed:
            continue
        _write(p, "\n".join(lines))
        return {"class": "budget_overspend", "file": _rel(root, p), "location": f"column {hdr[ci]}",
                "severity": "decision-critical",
                "description": f"{_rel(root, p)}: `{hdr[ci]}` scaled x1.55 on {changed} funded rows; total exceeds the budget.",
                "terms": {"specific": [os.path.basename(p), hdr[ci], "1.55"],
                          "generic": ["budget", "overspend", "over budget", "envelope", "exceed", "more than",
                                      "sum of cost", "total cost"]}}
    return None


RATE_HINT = re.compile(r"(per_?1000|per_?1k|incidence|rate|prevalence|pfpr|per_?100k)", re.I)


def plant_unit_error(root, rng, used):
    csvs = _first(root, ["data/processed/*.csv"]) + _first(root, ["data/**/*.csv"])
    seen, cands = set(), []
    for p in csvs:
        if p in seen or "allocation" in p:
            continue
        seen.add(p)
        lines = _read(p).split("\n")
        hdr = lines[0].split(",")
        for ci, h in enumerate(hdr):
            if not RATE_HINT.search(h):
                continue
            vals = []
            for j in range(1, min(len(lines), 400)):
                c = lines[j].split(",")
                if len(c) == len(hdr):
                    try:
                        vals.append(float(c[ci]))
                    except ValueError:
                        pass
            if len(vals) >= 5 and any(v > 0 for v in vals):
                cands.append((p, ci, h))
    if not cands:
        return None
    p, ci, h = rng.choice(cands)
    lines = _read(p).split("\n")
    n = 0
    for j in range(1, len(lines)):
        c = lines[j].split(",")
        if len(c) == len(hdr := lines[0].split(",")):
            try:
                c[ci] = _fmt_like(c[ci], float(c[ci]) * 100); lines[j] = ",".join(c); n += 1
            except ValueError:
                pass
    _write(p, "\n".join(lines))
    return {"class": "unit_error", "file": _rel(root, p), "location": f"column {h}", "severity": "decision-critical",
            "description": f"{_rel(root, p)}: `{h}` multiplied by 100 on {n} rows (unit change, header unchanged).",
            "terms": {"specific": [h, os.path.basename(p)],
                      "generic": ["unit", "per 1,000", "per 100,000", "order of magnitude", "100x", "100-fold",
                                  "implausib", "scale", "too high", "exceeds 100"]}}


_CITE = re.compile(r"([A-Z][A-Za-z'\-]+)(?: et al\.?|,? (?:and|&) [A-Z][A-Za-z'\-]+)?,?\s*\(?((?:19|20)\d\d)\)?")


def plant_citation_mismatch(root, rng, used):
    for p in _first(root, ["citations.md"]):
        lines = _read(p).split("\n")
        cands = []
        for i, l in enumerate(lines):
            if not l.strip() or l.lstrip().startswith(("#", "|", "```", "-  ")) and "et al" not in l:
                continue
            m = _CITE.search(l)
            if m and ("et al" in l or "doi" in l.lower() or "journal" in l.lower() or "lancet" in l.lower()):
                cands.append((i, m))
        if not cands:
            continue
        i, m = rng.choice(cands)
        old = m.group(2)
        new = str(int(old) - 6)
        lines[i] = lines[i][:m.start(2)] + new + lines[i][m.end(2):]
        _write(p, "\n".join(lines))
        return {"class": "citation_mismatch", "file": _rel(root, p), "location": f"line {i + 1}", "severity": "minor",
                "description": f"citations.md line {i + 1}: {m.group(1)} year {old} changed to {new}.",
                "terms": {"specific": [m.group(1), new],
                          "generic": ["citation", "year", "mismatch", "incorrect", "doi", "misattribut",
                                      "publication", "wrong"]}}
    return None


def plant_sensitivity_contradiction(root, rng, used):
    sa = _first(root, ["models/sensitivity_analysis.yaml", "sensitivity_analysis.yaml"])
    res = _first(root, ["results.md"])
    if not sa or not res or "results.md" in used.get("files", set()):
        return None
    m = re.search(r"^verdict:\s*['\"]?([A-Z_]+)", _read(sa[0]), re.M)
    if not m:
        return None
    v = m.group(1)
    claim = "ROBUST" if v in ("SENSITIVE", "UNSTABLE") else "UNSTABLE"
    lines = _read(res[0]).split("\n")
    idx = next((i for i, l in enumerate(lines) if re.search(r"sensitiv", l, re.I) and not l.lstrip().startswith("|")), None)
    if idx is None:
        return None
    sentence = (f"The sensitivity analysis verdict is {claim}: no tested perturbation changes the primary "
                f"recommendation." if claim == "ROBUST" else
                f"The sensitivity analysis verdict is {claim}: several perturbations change the primary recommendation.")
    lines.insert(idx + 1, sentence)
    _write(res[0], "\n".join(lines))
    return {"class": "sensitivity_contradiction", "file": _rel(root, res[0]), "location": f"line {idx + 2}",
            "severity": "major",
            "also_files": [_rel(root, sa[0])],
            "description": f"results.md asserts sensitivity verdict {claim}; {_rel(root, sa[0])} says {v}.",
            "terms": {"specific": [f"verdict is {claim}", claim],
                      "generic": ["sensitivity", "contradict", "inconsistent", "verdict", "does not match",
                                  "sensitivity_analysis", v.lower()]}}


_PROD = re.compile(r"\(\s*1\s*-\s*([\w.\[\]'\"]+)\s*\)\s*\*\s*\(\s*1\s*-\s*([\w.\[\]'\"]+)\s*\)")


def plant_double_counting(root, rng, used):
    """Replace a multiplicative combination of two effects with an additive one,
    only in an assignment line of model code (never prints/logs)."""
    cands = []
    for p in _model_code(root):
        for i, l in enumerate(_read(p).split("\n")):
            if _ASSIGN.match(l) and not re.search(r"\b(print|log|logger|f\")", l) and _PROD.search(l):
                cands.append((p, i))
    if not cands:
        return None
    p, i = rng.choice(cands)
    lines = _read(p).split("\n")
    m = _PROD.search(lines[i])
    lines[i] = lines[i][:m.start()] + f"(1 - ({m.group(1)} + {m.group(2)}))" + lines[i][m.end():]
    _write(p, "\n".join(lines))
    return {"class": "double_counting", "file": _rel(root, p), "location": f"line {i + 1}",
            "severity": "decision-critical",
            "description": f"{_rel(root, p)}:{i + 1}: multiplicative combination of `{m.group(1)}` and `{m.group(2)}` "
                           f"replaced by an additive one (overlapping effects double-counted).",
            "terms": {"specific": [f"{os.path.basename(p)}:{i + 1}", m.group(1), m.group(2)],
                      "generic": ["double count", "double-count", "additive", "multiplicative", "overlap",
                                  "combined effect", "independen", "sum of"]}}


_DATASET = re.compile(r"\b(DHS|MIS|NMIS|survey|WorldPop|MAP|census|GBD|WUENIC|MICS)\b", re.I)


def plant_data_vintage(root, rng, used):
    for p in _first(root, ["data_provenance.md", "data_quality.md"]):
        lines = _read(p).split("\n")
        cands = [(i, m) for i, l in enumerate(lines) if _DATASET.search(l) and "http" not in l and "`" not in l
                 for m in re.finditer(r"\b(201[5-9]|202[0-4])\b", l)]
        if not cands:
            continue
        i, m = rng.choice(cands)
        new = str(int(m.group(1)) - 14)
        ds = _DATASET.search(lines[i]).group(1)
        lines[i] = lines[i][:m.start()] + new + lines[i][m.end():]
        _write(p, "\n".join(lines))
        return {"class": "data_vintage", "file": _rel(root, p), "location": f"line {i + 1}", "severity": "major",
                "description": f"{_rel(root, p)} line {i + 1}: {ds} data year {m.group(1)} changed to {new}.",
                "terms": {"specific": [new, ds],
                          "generic": ["vintage", "outdated", "old data", "stale", "decision year", "years old",
                                      "out of date", "out-of-date"]}}
    return None


# --------------------------------------------------------------------------- Phase 42 γ classes
# Each targets one generic rule (D1-D5): mechanism manifest, severity floor,
# data-use audit, evidence for "addressed", transfer check.

def plant_mechanism_untested(root, rng, used):
    """Drop the sufficiency-test result from a simplified mechanism."""
    for p in _first(root, ["models/mechanism_manifest.yaml"]):
        lines = _read(p).split("\n")
        for i, l in enumerate(lines):
            m = re.match(r"^(\s*)sufficiency_test:\s*(\S.*)$", l)
            if not m:
                continue
            ref = m.group(2).strip()
            del lines[i]
            _write(p, "\n".join(lines))
            return {"class": "mechanism_untested", "file": _rel(root, p), "location": f"line {i + 1}",
                    "severity": "major",
                    "description": f"{_rel(root, p)}: the sufficiency-test result `{ref}` of a simplified "
                                   "mechanism removed (simplification left untested).",
                    "terms": {"specific": [ref, os.path.basename(ref)],
                              "generic": ["sufficiency test", "simplified", "untested", "mechanism",
                                          "simplification", "not tested"]}}
    return None


def _concern_blocks(lines):
    """[(start, end, severity)] for `- id:` concern items in a pre_mortem.yaml."""
    starts = [i for i, l in enumerate(lines) if re.match(r"^\s*-\s+id:\s*\S", l)]
    out = []
    for n, s in enumerate(starts):
        e = starts[n + 1] if n + 1 < len(starts) else len(lines)
        ms = [re.match(r"^\s*(?:-\s+)?severity:\s*['\"]?(\w+)", l) for l in lines[s:e]]
        sev = next((m.group(1).upper() for m in ms if m), None)
        out.append((s, e, sev))
    return out


def plant_prior_severity_below(root, rng, used):
    """Make a LOW/MEDIUM concern cite a registry prior graded above it."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from lib import expert_priors as _ep
        priors = sorted(_ep.load_priors(), key=lambda p: p.id)
    except Exception:  # noqa: BLE001
        return None
    rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    for p in _first(root, ["pre_mortem.yaml"]):
        lines = _read(p).split("\n")
        for s, e, sev in _concern_blocks(lines):
            if sev not in ("LOW", "MEDIUM"):
                continue
            higher = [q for q in priors if rank.get(q.severity_if_unaddressed, 1) > rank[sev]]
            if not higher or any("expert_prior:" in l for l in lines[s:e]):
                continue
            for i in range(s, e):
                if re.match(r"^\s*evidence_files:\s*$", lines[i]) and i + 1 < e \
                        and re.match(r"^\s*-\s+\S", lines[i + 1]):
                    q = higher[0]
                    indent = re.match(r"^(\s*-\s+)", lines[i + 1]).group(1)
                    old = lines[i + 1].strip()
                    lines[i + 1] = f"{indent}expert_prior:{q.id}"
                    _write(p, "\n".join(lines))
                    cid = lines[s].split("id:", 1)[1].strip()
                    return {"class": "prior_severity_below", "file": _rel(root, p), "location": cid,
                            "severity": "major",
                            "description": f"{_rel(root, p)} {cid}: graded {sev} but now cites prior `{q.id}` "
                                           f"({q.severity_if_unaddressed}) in place of `{old}`.",
                            "terms": {"specific": [cid, q.id],
                                      "generic": ["severity", "graded", "prior", "below", "downgraded",
                                                  "under-graded"]}}
    return None


_SENS_HINT = re.compile(r"sensitivity|robust|plausible range|vary|varied", re.I)


def plant_prose_closure(root, rng, used):
    """Close a concern that asks for a sensitivity check with prose only."""
    for p in _first(root, ["pre_mortem.yaml"]):
        lines = _read(p).split("\n")
        for s, e, sev in _concern_blocks(lines):
            block = "\n".join(lines[s:e])
            if not re.search(r"suggested_check:", block) or not _SENS_HINT.search(block):
                continue
            for i in range(s, e):
                if not re.match(r"^\s*addressed_in:\s*$", lines[i]):
                    continue
                for j in range(i + 1, e):
                    m = re.match(r"^\s*-\s+['\"]?([^'\"]+?)['\"]?\s*$", lines[j])
                    if not m:
                        break
                    ref = m.group(1)
                    if not re.search(r"\.(?:md|txt)\b", ref, re.I):
                        del lines[j]
                        _write(p, "\n".join(lines))
                        cid = lines[s].split("id:", 1)[1].strip()
                        return {"class": "prose_closure", "file": _rel(root, p), "location": cid,
                                "severity": "major",
                                "description": f"{_rel(root, p)} {cid}: the evidence artifact `{ref}` removed from "
                                               "addressed_in; the sensitivity check is closed by prose only.",
                                "terms": {"specific": [cid, ref],
                                          "generic": ["addressed", "prose", "no artifact", "evidence",
                                                      "sensitivity", "not run"]}}
    return None


def plant_unused_column(root, rng, used):
    """Add a data column that no model code reads."""
    taken = used.get("files", set()) if isinstance(used, dict) else set()
    csvs = [p for p in _first(root, ["data/processed/*.csv"]) + _first(root, ["data/*.csv"])
            if _rel(root, p) not in taken]
    for p in csvs:
        lines = _read(p).rstrip("\n").split("\n")
        if len(lines) < 3 or '"' in lines[0]:
            continue
        col = "subgroup_share_unread"
        lines = [lines[0] + "," + col] + [l + f",{0.2 + 0.05 * (k % 5):.2f}" if l else l
                                          for k, l in enumerate(lines[1:])]
        _write(p, "\n".join(lines) + "\n")
        return {"class": "unused_column", "file": _rel(root, p), "location": f"column {col}", "severity": "major",
                "description": f"{_rel(root, p)}: column `{col}` added that no model code reads.",
                "terms": {"specific": [col], "generic": ["unused", "not read", "ignored", "data column",
                                                         "never used", "collapsed"]}}
    return None


def plant_tuned_without_transfer(root, rng, used):
    """Declare a benchmark tuned, without a transfer to the target setting."""
    for p in _first(root, ["models/benchmark_targets.yaml"]):
        s = _read(p)
        if "transfer:" in s or "tuned_parameters:" in s:
            continue
        lines = s.split("\n")
        for i, l in enumerate(lines):
            m = re.match(r"^(\s*)-\s+id:\s*(\S+)", l)
            if not m:
                continue
            lines.insert(i + 1, f"{m.group(1)}  tuned_parameters: [effect_scale]")
            _write(p, "\n".join(lines))
            return {"class": "tuned_without_transfer", "file": _rel(root, p), "location": m.group(2),
                    "severity": "major",
                    "description": f"{_rel(root, p)} {m.group(2)}: declared tuned (effect_scale) with no "
                                   "transfer to the target setting's covariates.",
                    "terms": {"specific": [m.group(2), "effect_scale"],
                              "generic": ["tuned", "transfer", "target setting", "covariates", "benchmark site",
                                          "external validity"]}}
    return None


PLANTERS = [plant_zero_width_ci, plant_ci_inverted, plant_ledger_mismatch, plant_prose_stale_number,
            plant_benefit_total_population, plant_renamed_verdict, plant_budget_overspend, plant_unit_error,
            plant_citation_mismatch, plant_sensitivity_contradiction, plant_double_counting, plant_data_vintage,
            plant_mechanism_untested, plant_prior_severity_below, plant_prose_closure, plant_unused_column,
            plant_tuned_without_transfer]
CLASSES = [f.__name__[len("plant_"):] for f in PLANTERS]


# --------------------------------------------------------------------------- variants

def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def applicable(inputs: str, seed: int) -> list[str]:
    """Classes that find a target in this fixture (tried on a throwaway copy)."""
    out = []
    with tempfile.TemporaryDirectory() as td:
        for f, cls in zip(PLANTERS, CLASSES):
            d = os.path.join(td, cls)
            shutil.copytree(inputs, d)
            if f(d, random.Random(seed), {}) is not None:
                out.append(cls)
    return out


def build_variants(src_fixture: str, out_root: str, k: int = 3, per_variant: int = 3, seed: int = 7) -> list[dict]:
    fid = os.path.basename(os.path.normpath(src_fixture))
    meta = yaml.safe_load(open(os.path.join(src_fixture, "fixture.yaml")))
    rng = random.Random(f"{seed}:{fid}")
    app = applicable(os.path.join(src_fixture, "inputs"), seed)
    rng.shuffle(app)
    truth_dir = os.path.join(out_root, "_truth")
    os.makedirs(truth_dir, exist_ok=True)
    made = []
    # clean
    order = [("clean", [])]
    pool = list(app)
    for v in range(1, k + 1):
        pick = [pool[(v - 1) * per_variant + j] for j in range(per_variant) if (v - 1) * per_variant + j < len(pool)]
        if len(pick) < per_variant:                        # wrap around for more coverage
            extra = [c for c in app if c not in pick]
            rng.shuffle(extra)
            pick += extra[:per_variant - len(pick)]
        order.append((f"p{v}", pick))
    for tag, classes in order:
        vid = f"{fid}__{tag}"
        dest = os.path.join(out_root, vid)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(src_fixture, dest)
        inp = os.path.join(dest, "inputs")
        defects, used = [], {"files": set()}
        for n, cls in enumerate(classes, 1):
            f = PLANTERS[CLASSES.index(cls)]
            snap = {p: _sha(p) for p in glob.glob(os.path.join(inp, "**", "*"), recursive=True) if os.path.isfile(p)}
            d = f(inp, random.Random(f"{seed}:{vid}:{cls}"), used)
            if d is None or d["file"] in used["files"]:
                if d is not None:                          # overlapping file: undo by re-copying it
                    shutil.copy2(os.path.join(src_fixture, "inputs", d["file"]), os.path.join(inp, d["file"]))
                continue
            changed = [p for p, h in snap.items() if _sha(p) != h]
            if len(changed) != 1:
                raise RuntimeError(f"{vid}/{cls}: expected one changed file, got {changed}")
            used["files"].add(d["file"])
            d["defect_id"] = f"{vid}#{n}"
            defects.append(d)
        m = dict(meta)
        m["id"] = vid
        m["planted_variant_of"] = fid
        m["variant_tag"] = tag
        for d in defects:                                  # keep hashes truthful; no defect detail here
            m["files"][d["file"]] = {"sha256": _sha(os.path.join(inp, d["file"])),
                                     "bytes": os.path.getsize(os.path.join(inp, d["file"]))}
        yaml.safe_dump(m, open(os.path.join(dest, "fixture.yaml"), "w"), sort_keys=False)
        truth = {"variant": vid, "source_fixture": fid, "tag": tag, "seed": seed, "defects": defects}
        yaml.safe_dump(truth, open(os.path.join(truth_dir, f"{vid}.yaml"), "w"), sort_keys=False)
        made.append(truth)
    return made


# --------------------------------------------------------------------------- self-test

def _mini_fixture(root):
    inp = os.path.join(root, "F", "inputs")
    os.makedirs(os.path.join(inp, "models")); os.makedirs(os.path.join(inp, "data", "processed"))
    _write(os.path.join(inp, "uncertainty_report.yaml"),
           "n_draws: 200\nscalar_outputs:\n  total_dalys_averted:\n    mean: 1000.0\n    ci_low: 800.0\n"
           "    ci_high: 1300.0\n  total_cost:\n    mean: 50.0\n    ci_low: 40.0\n    ci_high: 60.0\n")
    _write(os.path.join(inp, "models", "claims_ledger.yaml"),
           "claims:\n  - id: dalys_total\n    claim_kind: scalar\n    value: 1000\n"
           "    source_artifact: uncertainty_report.yaml\n    source_field: scalar_outputs.total_dalys_averted.mean\n")
    _write(os.path.join(inp, "results.md"),
           "# Results\n\nThe optimized allocation averts 1,000 DALYs per year across all districts studied.\n"
           "## Sensitivity\nSensitivity analysis on load-bearing parameters is reported below in detail.\n")
    _write(os.path.join(inp, "models", "model.py"),
           "population = 10\npop_u5 = 2\ncases_averted = pop_u5 * incidence * eff\n"
           "combined = 1 - (1 - eff_a) * (1 - eff_b)\n")
    _write(os.path.join(inp, "model_comparison_formal.yaml"), "models: []\nverdict: CLEAN\n")
    _write(os.path.join(inp, "models", "sensitivity_analysis.yaml"), "load_bearing_parameters: []\nverdict: SENSITIVE\n")
    _write(os.path.join(inp, "models", "allocation_optimized.csv"),
           "unit,package,cost,dalys_averted\nA,x,100.0,5\nB,y,0.0,0\nC,x,200.0,9\n")
    _write(os.path.join(inp, "data", "processed", "rates.csv"),
           "unit,incidence_per_1000\n" + "\n".join(f"u{i},{10 + i}.5" for i in range(8)) + "\n")
    _write(os.path.join(inp, "citations.md"), "# Citations\n- Smith et al. (2019). A trial. The Lancet. doi:10/x\n")
    _write(os.path.join(inp, "data_provenance.md"), "# Provenance\nDHS survey 2021 state estimates were used.\n")
    # Phase 42 γ targets.
    _write(os.path.join(inp, "models", "mechanism_manifest.yaml"),
           "mechanisms:\n- id: age_profile\n  status: simplified\n  assumption: one profile\n"
           "  sufficiency_test: models/sufficiency_age.yaml\n")
    _write(os.path.join(inp, "pre_mortem.yaml"),
           "concerns:\n- id: PM-001\n  severity: LOW\n  concern: c\n"
           "  suggested_check: Confirm robust to a plausible range.\n"
           "  evidence_files:\n  - plan.md\n"
           "  addressed_in:\n  - modeling_strategy.md § R\n  - models/sensitivity_analysis.yaml\n")
    _write(os.path.join(inp, "data", "units.csv"), "unit,pop\nA,1\nB,2\nC,3\n")
    _write(os.path.join(inp, "models", "benchmark_targets.yaml"),
           "benchmarks:\n- id: B1\n  target_value: 1.0\n  computed_value: 1.0\n")
    yaml.safe_dump({"id": "F", "stage": "CRITIQUE", "files": {}}, open(os.path.join(root, "F", "fixture.yaml"), "w"))
    open(os.path.join(root, "F", "spawn_prompt.md"), "w").write("This is critique round 1. Run directory: {run_dir}")
    return os.path.join(root, "F")


def _self_test() -> int:
    failures = []
    def ok(c, m):
        if not c: failures.append(m)
    with tempfile.TemporaryDirectory() as td:
        fx = _mini_fixture(td)
        app = applicable(os.path.join(fx, "inputs"), 7)
        ok(set(app) == set(CLASSES), f"T1 every class applies to the mini fixture: missing {set(CLASSES) - set(app)}")
        # each planter: one file changed, manifest fields, specific term present in the edited file
        for f, cls in zip(PLANTERS, CLASSES):
            d = os.path.join(td, "one", cls)
            shutil.copytree(os.path.join(fx, "inputs"), d)
            before = {p: _sha(p) for p in glob.glob(os.path.join(d, "**", "*"), recursive=True) if os.path.isfile(p)}
            m = f(d, random.Random(1), {})
            changed = [p for p, h in before.items() if _sha(p) != h]
            ok(m and len(changed) == 1 and m["class"] == cls and m["severity"] in
               ("decision-critical", "major", "minor") and m["terms"]["specific"] and m["terms"]["generic"],
               f"T2 {cls}: manifest/changed={changed}")
        # determinism and non-overlap
        out1, out2 = os.path.join(td, "o1"), os.path.join(td, "o2")
        a = build_variants(fx, out1, k=3, per_variant=3, seed=7)
        b = build_variants(fx, out2, k=3, per_variant=3, seed=7)
        ok([[d["class"] for d in t["defects"]] for t in a] == [[d["class"] for d in t["defects"]] for t in b],
           "T3 deterministic for a seed")
        ok(a[0]["tag"] == "clean" and a[0]["defects"] == [], "T4 clean variant has no defects")
        for t in a[1:]:
            files = [d["file"] for d in t["defects"]]
            ok(len(files) == len(set(files)) and len(files) == 3, f"T5 3 non-overlapping files in {t['variant']}: {files}")
        ok(all(os.path.exists(os.path.join(out1, "_truth", f"{t['variant']}.yaml")) for t in a), "T6 truth files written")
        leaked = [p for p in glob.glob(os.path.join(out1, "F__p1", "**", "*"), recursive=True)
                  if os.path.isfile(p) and "zero_width_ci" in _read(p) + ""]
        ok(not leaked and not os.path.exists(os.path.join(out1, "F__p1", "inputs", "_truth")),
           f"T7 no answer key inside the fixture: {leaked}")
        clean_res = _read(os.path.join(out1, "F__clean", "inputs", "results.md"))
        ok(clean_res == _read(os.path.join(fx, "inputs", "results.md")), "T8 clean variant identical to source")
    if failures:
        print("SELF-TEST FAILURES:", *failures, sep="\n  - "); return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", choices=["build", "list"])
    ap.add_argument("--fixtures")
    ap.add_argument("--out")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--per-variant", type=int, default=3)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if a.cmd == "list":
        for p in sorted(glob.glob(os.path.join(a.out, "_truth", "*.yaml"))):
            t = yaml.safe_load(open(p))
            print(f"{t['variant']:24} {[(d['class'], d['severity'], d['file']) for d in t['defects']]}")
        return 0
    srcs = sorted(d for d in glob.glob(os.path.join(a.fixtures, "*")) if os.path.exists(os.path.join(d, "fixture.yaml")))
    if a.only:
        srcs = [s for s in srcs if os.path.basename(s) in a.only]
    for s in srcs:
        made = build_variants(s, a.out, a.k, a.per_variant, a.seed)
        print(f"{os.path.basename(s)}: " + "; ".join(f"{t['tag']}={[d['class'] for d in t['defects']]}" for t in made))
    return 0


if __name__ == "__main__":
    sys.exit(main())
