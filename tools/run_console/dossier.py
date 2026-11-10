#!/usr/bin/env python3
"""The run dossier: what a run is made of, for the run console (docs/design/v2-run-console.md §15).

    python3 tools/run_console/dossier.py --self-test

Four read-only tabs beside Stream and Figures: Spec, Evidence, Engine and Calibration. Each is built from files the
harness already writes, read through the console's confined reader (run_console._load_rel: a plain relative path,
no link at any component, one no-follow fd). Paths named inside artifacts are untrusted and go through the same
reader. No harness loader runs and no engine is imported. The one read outside the run directory is this checkout's
toolkit/engines/<name>/benchmarks/results.yaml, only when its sha256 equals the one the run recorded. Every part
is data or {"available": False, "why": ...}; the page shows the reason, never an empty panel. Nothing is written.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import run_console as RC  # noqa: E402
from lib import safe_io  # noqa: E402  (module attributes, so a self-test can trace every no-follow open)

REPO = RC.REPO
JS = os.path.join(HERE, "dossier.js")
ENGINE_YAML, CHOICE = "analysis/engine.yaml", "engine_choice.json"
REGISTRY, PROVENANCE, DERIVATIONS = "evidence/registry.yaml", "data/inputs_provenance.json", "data/derivations.yaml"
INPUT_CHECKS, CALIBRATION, EMULATOR = "data/input_checks.json", "analysis/calibration.yaml", "analysis/emulator.yaml"
ARCHIVE_DIR, ARCHIVE_PREFIX = "evidence_planner", "superseded_signoff_"
# the run's record of the engine's benchmark results file, as analysis/engine.yaml `inputs` names it
_RESULTS_KEY = re.compile(r"^(?:\.\./)*toolkit/engines/([a-z0-9_]+)/benchmarks/results\.yaml$")
_NOT_YET = "not written by this run (yet)"


def na(why: str) -> dict:
    """A part that cannot be shown, and why."""
    return {"available": False, "why": why}


# ------------------------------------------------------------------ confined reads (run_console's read model)
def _doc(run_dir: str, rel: str):
    """(parsed, None) or (None, why) of a run artifact through run_console._load_rel."""
    try:
        RC.check_rel(rel)
    except ValueError as e:
        return None, str(e)
    if not RC._rexists(run_dir, rel):
        return None, f"{rel}: {_NOT_YET}"
    e = RC._load_rel(run_dir, rel)
    return (e["parsed"], None) if e["err"] is None else (None, f"{rel}: {e['err']}")


def _entry(run_dir: str, rel: str):
    """run_console._load_rel's entry (parsed value and sha256 from one read), or None for a refused or missing path."""
    try:
        RC.check_rel(rel)
    except ValueError:
        return None
    return RC._load_rel(run_dir, rel) if RC._rexists(run_dir, rel) else None


_TYPES = {dict: "a mapping", list: "a list", str: "text", (int, float): "a number", bool: "true or false"}


def _invalid(doc, rel: str, need) -> str | None:
    """Why `doc` lacks a required field, or None. `need`: [(dotted path, type)]. A payload without its fields is not
    shown as zeros."""
    for path, typ in need:
        v = doc
        for k in path.split("."):
            v = v.get(k) if isinstance(v, dict) else None
        if v is None:
            return f"{rel}: missing {path}"
        if not isinstance(v, typ) or (typ is not bool and isinstance(v, bool)):
            return f"{rel}: invalid {path} (not {_TYPES.get(typ, typ)})"
    return None


def _need(run_dir: str, rel: str, need):
    """(doc, None) or (None, why): a mapping with every required field."""
    doc, why = _mapping(run_dir, rel)
    return (None, why) if why else (None, _invalid(doc, rel, need)) if _invalid(doc, rel, need) else (doc, None)


def _mapping(run_dir: str, rel: str):
    doc, why = _doc(run_dir, rel)
    if why is None and not isinstance(doc, dict):
        return None, f"{rel}: not a mapping"
    return doc, why


def _sha(run_dir: str, rel: str):
    """(sha256, None) or (None, why)."""
    try:
        RC.check_rel(str(rel))
    except ValueError as e:
        return None, str(e)
    if not RC._rexists(run_dir, rel):
        return None, "missing"
    e = RC._load_rel(run_dir, rel)
    return (e["sha256"], None) if e["sha256"] else (None, e["err"])


def _state(run_dir: str, rel: str, want) -> tuple[str, str | None]:
    """match | changed | missing | refused (a path outside the run dir, a link, a credential file)."""
    sha, why = _sha(run_dir, rel)
    if sha is None:
        return ("missing", "not in the run directory") if why == "missing" else ("refused", why)
    return ("match" if want and sha == want else "changed"), None


def _listing(run_dir: str, rel: str = "") -> list:
    """[(name, is_dir)] of a directory of the run, opened with no link followed; [] when absent or refused."""
    try:
        parts = [p for p in rel.split("/") if p] if rel else []
        if parts:
            RC.check_rel(rel)
        fd = safe_io.dir_fd_nofollow(os.path.realpath(run_dir), parts)
    except (OSError, ValueError):
        return []
    try:
        with os.scandir(fd) as it:
            return sorted((e.name, e.is_dir(follow_symlinks=False)) for e in it)
    finally:
        os.close(fd)


def _walk(run_dir: str, rel: str, depth: int = 5) -> list:
    """Every file under a run directory, no link followed (a link is listed, then refused when read)."""
    out = []
    for name, is_dir in _listing(run_dir, rel):
        sub = f"{rel}/{name}" if rel else name
        if is_dir and depth > 0:
            out += _walk(run_dir, sub, depth - 1)
        elif not is_dir:
            out.append(sub)
    return out


def _rows(run_dir: str, rel: str):
    """(header, rows, None) of a CSV in the run dir, or (None, None, why)."""
    text, why = _doc(run_dir, rel)
    if why:
        return None, None, why
    if not isinstance(text, str):
        return None, None, f"{rel}: not a text table"
    r = csv.reader(io.StringIO(text))
    header = next(r, None)
    return header or [], [dict(zip(header or [], row)) for row in r if row], None


def _m(v) -> dict:
    return v if isinstance(v, dict) else {}


def _l(v) -> list:
    return v if isinstance(v, list) else []


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _short(v) -> str:
    """A leaf for a table cell: scalars as is, a list joined, a mapping as key: value pairs."""
    if isinstance(v, dict):
        return "; ".join(f"{k}: {_short(x)}" for k, x in v.items())
    if isinstance(v, list):
        return ", ".join(_short(x) for x in v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return "" if v is None else str(v)


# ------------------------------------------------------------------ Spec
def _units(run_dir: str, du: dict) -> dict:
    out = {k: du.get(k) for k in ("csv", "id", "label", "region")}
    rel = du.get("csv")
    if not isinstance(rel, str):
        return {**out, "count": None, "count_why": "the spec names no units table (decision_units.csv)"}
    header, rows, why = _rows(run_dir, rel)
    if why:
        return {**out, "count": None, "count_why": why}
    reg = du.get("region")
    regions = len({r.get(reg) for r in rows}) if reg in header else None
    return {**out, "count": len(rows), "regions": regions, "columns": len(header),
            "count_why": None if regions is not None or not reg else f"{rel} has no region column {reg}"}


def _binding_of(b) -> dict:
    if not isinstance(b, dict):
        return {"how": "value", "detail": _short(b)}
    if "columns" in b:
        c = b["columns"]
        detail = ", ".join(f"{k} ← {v}" if k != v else str(k) for k, v in c.items()) if isinstance(c, dict) else _short(c)
        return {"how": "columns", "detail": detail, "source": b.get("source"), "split": b.get("split")}
    if "column" in b:
        return {"how": "column", "detail": _short(b["column"]), "source": b.get("source"), "split": b.get("split")}
    if "value" in b:
        return {"how": "value", "detail": _short(b["value"]), "source": b.get("source"), "split": b.get("split")}
    return {"how": "other", "detail": _short(b), "source": b.get("source"), "split": b.get("split")}


def _bindings(du: dict) -> list:
    return [{"attribute": str(k), **_binding_of(v)} for k, v in _m(du.get("engine_attributes")).items()]


def _factor(it: dict, prices: dict):
    """The price-year factor of toolkit/alloc/costs.py _factor, or None when the spec lacks an entry."""
    rule = it.get("price_rule")
    if rule == "as_published":
        return 1.0
    b, py = str(prices.get("base_year")), str(it.get("price_year"))
    try:
        if rule == "index":
            idx = {str(k): float(v) for k, v in _m(prices.get("index")).items()}
            return idx[b] / idx[py]
        if rule == "local_index":
            loc = _m(prices.get("local"))
            xr = {str(k): float(v) for k, v in _m(loc.get("exchange_rate")).items()}
            li = {str(k): float(v) for k, v in _m(loc.get("index")).items()}
            return xr[py] / xr[b] * li[b] / li[py]
    except (KeyError, ValueError, ZeroDivisionError, TypeError):
        return None
    return None


PER = {"per_person": "per person", "per_eligible": "per eligible person", "per_reached": "per person reached",
       "per_commodity": "per person covered", "fixed": "per unit receiving it", "per_unit": "per unit receiving it",
       "per_outcome": "per outcome"}


def _cost_items(opts: dict) -> list:
    prices, comps = _m(opts.get("prices")), _m(opts.get("components"))
    out = []
    for it in _l(opts.get("cost_items")):
        if not isinstance(it, dict):
            continue
        v, basis = _num(it.get("value")), it.get("basis")
        f = _factor(it, prices)
        base = v * f if v is not None and f is not None else None
        mult = 1.0
        for k in ("share", "times", "years"):
            mult *= _num(it.get(k)) if _num(it.get(k)) is not None else 1.0
        per = None
        if base is not None and basis in ("per_person", "per_eligible", "per_reached", "per_commodity"):
            per = base * mult
            if basis == "per_commodity":
                ppc = _num(it.get("people_per_commodity"))
                per = per / ppc if ppc else None
        extra = []
        if basis == "per_reached":
            rs = _num(_m(comps.get(it.get("component"))).get("reach_share"))
            extra.append(f"reach share {rs} of the eligible" if rs is not None else "no reach share in the spec")
        if it.get("people_per_commodity") is not None:
            extra.append(f"{it['people_per_commodity']} people per commodity")
        for k, w in (("share", "share charged"), ("times", "times per year"), ("years", "years")):
            if it.get(k) is not None:
                extra.append(f"{w} {it[k]}")
        if basis == "per_outcome":
            extra.append(f"outcome {it.get('outcome')}" + (" (incremental)" if it.get("incremental") else ""))
        out.append({"id": it.get("id"), "component": it.get("component"), "value": v, "price_year": it.get("price_year"),
                    "rule": it.get("price_rule"), "basis": basis, "includes": _l(it.get("includes")),
                    "not_double_counted": [{"item": str(k), "reason": str(w)}
                                           for k, w in _m(it.get("not_double_counted")).items()],
                    "extra": extra, "factor": f, "base_value": base, "per_person": per, "per_label": PER.get(basis, basis),
                    "why_no_per_person": None if per is not None else
                    ("priced per outcome (a model output)" if basis == "per_outcome" else "a fixed amount"
                     if basis in ("fixed", "per_unit") else "the price year has no index entry" if f is None else "not computable")})
    return out


def _card_units(run_dir: str) -> dict:
    doc, _ = _mapping(run_dir, ENGINE_YAML)
    return {str(p.get("name")): p.get("units") for p in _l(_m(_m(doc).get("data")).get("parameters"))
            if isinstance(p, dict)}


def _decision_tests(spec: dict, units: dict) -> list:
    out = []
    for t in _l(_m(spec.get("analysis")).get("decision_tests")):
        if not isinstance(t, dict):
            continue
        typ, unit, values = t.get("type"), "", _l(t.get("values"))
        if typ == "parameter_value":
            varied, unit = f"parameter {t.get('parameter')}", units.get(str(t.get("parameter"))) or ""
        elif typ == "interaction_rescale":
            varied, unit = (f"interaction of {_short(t.get('components'))} with any of {_short(t.get('with_any'))}"
                            + (f" (benchmark {t['benchmark']})" if t.get("benchmark") else "")), "×"
        elif typ == "group_rule" and isinstance(t.get("add"), dict):
            a = t["add"]
            varied = (f"group rule added: {a.get('rule') or 'a rule'} on {_short(a.get('components'))} by "
                      f"{a.get('by') or '(no grouping column)'}" + (f" ({a['reason']})" if a.get("reason") else ""))
        elif typ == "group_rule":
            rid = t.get("rule")
            grs = _m(spec.get("constraints")).get("group_rules")    # the raw spec: a list of {id, ...}
            r = (next((g for g in grs if isinstance(g, dict) and g.get("id") == rid), {}) if isinstance(grs, list)
                 else _m(_m(grs).get(rid)))
            varied = (f"group rule {rid} removed" + (f" ({r.get('rule')} on {_short(r.get('components'))} by "
                                                     f"{r.get('by')})" if r else " (the spec's constraints do not "
                                                                                 "define it)"))
        elif typ == "envelope":
            varied, unit = "the envelope (the plan re-solved at each value; fixed costs unchanged)", "USD"
        else:
            varied = _short({k: v for k, v in t.items() if k not in ("id", "type", "text", "values", "finding")})
        out.append({"id": t.get("id"), "type": typ, "varied": varied, "values": values, "unit": unit,
                    "question": t.get("text"), "finding": t.get("finding")})
    return out


def _flatten(v, path: str, out: dict) -> dict:
    """A spec as path -> leaf text. Lists of mappings with an id are keyed by id, so a diff compares them by id."""
    if isinstance(v, dict):
        for k in v:
            _flatten(v[k], f"{path}.{k}" if path else str(k), out)
    elif isinstance(v, list) and v and all(isinstance(x, dict) and x.get("id") is not None for x in v):
        for x in v:
            _flatten(x, f"{path}.{x['id']}", out)
    else:
        out[path] = _short(v)
    return out


def spec_diff(before: dict, after: dict) -> list:
    """[{section, field, before, after}] over two parsed specs; a field one side lacks is None."""
    a, b = _flatten(before, "", {}), _flatten(after, "", {})
    return [{"section": k.split(".")[0], "field": k, "before": a.get(k), "after": b.get(k)}
            for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]


def _archived_spec(run_dir: str, folder: str, want) -> tuple:
    """(rel, parsed) of the spec under an archive folder whose sha256 is `want`, or (None, why)."""
    if not want:
        return None, "the archived record has no spec sha256"
    for rel in _walk(run_dir, folder, 3):
        name = rel.rsplit("/", 1)[-1]
        if not (name.startswith("decision_spec") and name.endswith((".yaml", ".yml"))):
            continue
        e = _entry(run_dir, rel)            # one read: the hash and the parsed spec come from the same bytes
        if e and e["sha256"] == want:
            return (rel, e["parsed"]) if e["err"] is None else (None, f"{rel}: {e['err']}")
    return None, f"no spec in {folder} has the archived sha256 {str(want)[:12]}"


def _record(rec: dict, rel: str) -> dict:
    shas = _m(rec.get("sha256"))
    return {"file": rel, "signed_by": rec.get("signed_by"), "signed_at": str(rec.get("signed_at") or ""),
            "note": rec.get("note"), "spec_sha256": shas.get(RC.SPEC), "derivations_sha256": shas.get(RC.DERIV_RECORD)}


def _signoffs(run_dir: str, spec: dict, spec_sha) -> list:
    archived = []
    for name, _ in _listing(run_dir, ARCHIVE_DIR):
        if not name.startswith(ARCHIVE_PREFIX):
            continue
        folder = f"{ARCHIVE_DIR}/{name}"
        rel = f"{folder}/{RC.SPEC_SIGNOFF}"
        rec, why = _mapping(run_dir, rel)
        if why:
            archived.append({"file": rel, "folder": folder, "signed_at": "", "available": False, "why": why})
            continue
        r = {**_record(rec, rel), "folder": folder, "current": False}
        r["spec_file"], r["_spec"] = _archived_spec(run_dir, folder, r["spec_sha256"])
        archived.append(r)
    archived.sort(key=lambda r: (r.get("signed_at") or "", r["file"]))
    cur, why = _mapping(run_dir, RC.SPEC_SIGNOFF)
    current = None
    if cur is not None:
        current = {**_record(cur, RC.SPEC_SIGNOFF), "current": True, "spec_file": RC.SPEC,
                   "matches": cur is not None and _m(cur.get("sha256")).get(RC.SPEC) == spec_sha}
        d_sha, _ = _sha(run_dir, RC.DERIV_RECORD)
        current["derivations_match"] = current["derivations_sha256"] == (d_sha if d_sha else None)
    elif why and _NOT_YET not in why:
        current = {"file": RC.SPEC_SIGNOFF, "current": True, "available": False, "why": why}
    nxt = [r for r in archived if r.get("available") is not False]
    for i, r in enumerate(nxt):
        after = nxt[i + 1] if i + 1 < len(nxt) else None
        target, label = (after.get("_spec"), "the next sign-off") if after else \
            (spec, "the current spec" + (" (signed)" if current and current.get("matches") else " (not signed)"))
        if not isinstance(r.get("_spec"), dict):
            r["diff"] = na(r["_spec"] if isinstance(r.get("_spec"), str) else "the archived spec cannot be read")
        elif not isinstance(target, dict):
            r["diff"] = na(f"the next spec cannot be read: {target}" if isinstance(target, str) else
                           "the next spec cannot be read")
        else:
            r["diff"] = {"available": True, "against": label, "against_at": after["signed_at"] if after else None,
                         "rows": spec_diff(r["_spec"], target)}
    for r in archived:
        r.pop("_spec", None)
    return archived + ([current] if current else [])


def spec_tab(run_dir: str) -> dict:
    spec, why = _need(run_dir, RC.SPEC, [("question", str), ("decision_units", dict)])
    if why:
        return na(why)
    sha, _ = _sha(run_dir, RC.SPEC)
    view = RC.spec_view(run_dir)
    du, opts = _m(spec.get("decision_units")), _m(spec.get("options"))
    obj, con, cal, unc = (_m(spec.get(k)) for k in ("objective", "constraints", "calibration", "uncertainty"))
    comps = _m(opts.get("components"))
    emap = _m(opts.get("engine_map"))
    pk = opts.get("packages")
    return {
        "available": True, "file": RC.SPEC, "sha256": sha, "status": view.get("status"), "status_error": view.get("error"),
        "question": spec.get("question"), "decision_maker": spec.get("decision_maker"),
        "decision_date": spec.get("decision_date"), "horizon": spec.get("horizon"),
        "units": _units(run_dir, du), "bindings": _bindings(du),
        "defaults": [{"name": str(k), "reason": _short(v)} for k, v in _m(du.get("defaults_accepted")).items()],
        "populations": [{"name": str(k), "column": _m(v).get("column", v if not isinstance(v, dict) else None),
                         "age_groups": _l(_m(v).get("age_groups"))} for k, v in _m(du.get("populations")).items()],
        "option_populations": [{"name": str(k), "column": _short(v)} for k, v in _m(opts.get("populations")).items()],
        "components": [{"id": str(k), "group": _m(v).get("group"), "eligible": _m(v).get("eligible"),
                        "engine": _short(emap.get(k)) if k in emap else None} for k, v in comps.items()],
        "packages": {"generate": bool(_m(pk).get("generate")),
                     "list": [{"name": str(k), "components": _l(v)} for k, v in _m(pk).items() if k != "generate"]},
        "prices": {"base_year": _m(opts.get("prices")).get("base_year"),
                   "index_name": _m(opts.get("prices")).get("index_name"),
                   "index": [[str(k), v] for k, v in _m(_m(opts.get("prices")).get("index")).items()]},
        "cost_items": _cost_items(opts),
        "objective": {"outcome": obj.get("outcome"), "sense": obj.get("sense"), "window": obj.get("horizon"),
                      "delivery": obj.get("delivery"), "discounting": obj.get("discounting")},
        "constraints": {"envelope_usd": con.get("envelope_usd"),
                        "fixed": [{"name": str(k), "usd": v} for k, v in _m(con.get("fixed_costs_usd")).items()],
                        "eligibility": [{"component": str(k), "rule": _short(v)} for k, v in
                                        _m(con.get("eligibility")).items()],
                        "other": {k: _short(v) for k, v in con.items()
                                  if k not in ("envelope_usd", "fixed_costs_usd", "eligibility")}},
        "comparator": {"package": _m(spec.get("comparator")).get("package"),
                       "text": _m(spec.get("comparator")).get("text")},
        "calibration": {"method": cal.get("method"), "attribute": cal.get("attribute"), "scale": cal.get("scale"),
                        "range": cal.get("range"), "target_column": cal.get("target_column"),
                        "window": cal.get("window"), "metric": cal.get("metric"),
                        "target_uncertainty": {k: _short(v) for k, v in _m(cal.get("target_uncertainty")).items()},
                        "target_frame": {k: _short(v) for k, v in _m(cal.get("target_frame")).items()}},
        "uncertainty": {"level": unc.get("level"), "draws": unc.get("draws"), "seed": unc.get("seed"),
                        "sources": [{"kind": _m(s).get("kind"),
                                     "items": [{"id": str(k), "range": v} for k, v in _m(_m(s).get("items")).items()]}
                                    for s in _l(unc.get("sources"))],
                        "excluded": _l(unc.get("excluded"))},
        "analysis": {k: _short(v) for k, v in _m(spec.get("analysis")).items() if k != "decision_tests"},
        "decision_tests": _decision_tests(spec, _card_units(run_dir)),
        "signoffs": _signoffs(run_dir, spec, sha),
    }


# ------------------------------------------------------------------ Evidence
_CIT_HEAD = re.compile(r"^##\s*\[([^\]]+)\]\s*(.*)$")
_CIT_FIELD = re.compile(r"^\s*-\s*\*\*([^*]+)\*\*:\s*(.*)$")


def _citations(run_dir: str) -> dict:
    files = sorted(r for r in _walk(run_dir, "data/inputs", 6) + [n for n, d in _listing(run_dir) if not d]
                   if re.match(r"citations_[^/]*\.txt$", r.rsplit("/", 1)[-1]))
    if not files:
        return na(f"{_NOT_YET}: no citations_*.txt under data/inputs or in the run directory")
    entries, errors = [], []
    for rel in files:
        text, why = _doc(run_dir, rel)
        if why or not isinstance(text, str):
            errors.append(why or f"{rel}: not text")
            continue
        cur = None
        for line in text.splitlines():
            m = _CIT_HEAD.match(line)
            if m:
                cur = {"id": m.group(1).strip(), "title": m.group(2).strip(), "fields": {}, "file": rel, "cited_by": []}
                entries.append(cur)
                continue
            f = _CIT_FIELD.match(line)
            if f and cur is not None:
                cur["fields"][f.group(1).strip()] = f.group(2).strip()
    return {"available": True, "files": files, "entries": entries, "errors": errors}


def _used_in(flat: dict, name: str) -> list:
    pat = re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])")
    return [k for k, v in flat.items() if isinstance(v, str) and pat.search(v)]


def _cited_ids(text) -> list:
    return re.findall(r"\b([A-Z]{1,3}\d+[a-z]?)\b", str(text or ""))


def _registry(run_dir: str, by_id: dict) -> dict:
    doc, why = _need(run_dir, REGISTRY, [("entries", list)])
    if why:
        return na(why)
    out = []
    for e in _l(doc.get("entries")):
        if not isinstance(e, dict):
            continue
        cit = e.get("citation")
        out.append({"id": e.get("id") or e.get("name") or e.get("parameter"), "value": e.get("value"),
                    "unit": e.get("units") or e.get("unit"), "source": e.get("source"), "citation": cit,
                    "citation_title": (by_id.get(str(cit)) or {}).get("title") if cit else None,
                    "used_in": _l(e.get("used_in") or e.get("where")) or ([e["used_by"]] if e.get("used_by") else []),
                    "status": e.get("status"),
                    "more": _short({k: v for k, v in e.items() if k not in ("id", "name", "parameter", "value", "units",
                                                                            "unit", "source", "citation", "used_in",
                                                                            "where", "used_by", "status")})})
    return {"available": True, "file": REGISTRY, "status": doc.get("status"), "stub": doc.get("status") == "stub",
            "entries": out}


def _param_registries(run_dir: str, flat: dict, by_id: dict) -> list:
    out = []
    for rel in _walk(run_dir, "data/inputs", 6):
        name = rel.rsplit("/", 1)[-1]
        if "registry" not in name or not name.endswith((".yaml", ".yml")):
            continue
        doc, why = _mapping(run_dir, rel)
        if why:
            out.append({"file": rel, **na(why)})
            continue
        if not isinstance(doc.get("parameters"), list):
            continue
        rows = []
        for p in doc["parameters"]:
            if not isinstance(p, dict) or p.get("name") is None:
                continue
            src = p.get("source")
            cits = [c for c in _cited_ids(src) if c in by_id]
            rows.append({"name": str(p["name"]), "value": p.get("value"),
                         "ci": [p.get("ci_low"), p.get("ci_high")] if p.get("ci_low") is not None else None,
                         "kind": p.get("kind"), "source": src, "subgroup": p.get("subgroup"),
                         "applies_to": p.get("applies_to"),
                         "citation_title": "; ".join(by_id[c]["title"] for c in cits) or None,
                         "used_in": _used_in(flat, str(p["name"]))})
        out.append({"available": True, "file": rel, "entries": rows})
    return out


def _inputs(run_dir: str, ders: list, ignored: dict) -> dict:
    doc, why = _need(run_dir, PROVENANCE, [("inputs", dict)])
    if why:
        return na(why)
    rows = []
    for rel, p in _m(doc.get("inputs")).items():
        p = _m(p)
        st, swhy = _state(run_dir, str(rel), p.get("sha256"))
        rows.append({"path": str(rel), "source": p.get("source"), "bytes": p.get("bytes"),
                     "sha256": p.get("sha256"), "state": st, "why": swhy,
                     "read_by": [str(d.get("id")) for d in ders if rel in _l(d.get("inputs"))],
                     "ignored": ignored.get(rel)})
    cited = []
    for rel, p in _m(doc.get("cited")).items():
        st, swhy = _state(run_dir, str(rel), _m(p).get("sha256"))
        cited.append({"path": str(rel), "sha256": _m(p).get("sha256"), "state": st, "why": swhy})
    return {"available": True, "file": PROVENANCE, "rows": rows, "cited": cited}


def _derivations(run_dir: str, ders: list, ignored: dict, why_d) -> dict:
    if why_d:
        return na(why_d)
    rec_doc, rwhy = _mapping(run_dir, RC.DERIV_RECORD)
    recs = _m(_m(rec_doc).get("derivations"))
    rows = []
    for d in ders:
        rec = _m(recs.get(d.get("id")))
        cols = _m(d.get("columns"))
        uses, ign = _m(cols.get("uses")), _m(cols.get("ignores"))
        ins = []
        for rel in _l(d.get("inputs")):
            st, swhy = _state(run_dir, str(rel), _m(rec.get("inputs")).get(rel))
            ins.append({"path": str(rel), "state": st, "why": swhy, "uses": [str(c) for c in _l(uses.get(rel))],
                        "ignores": [{"column": str(c), "reason": _short(r)} for c, r in _m(ign.get(rel)).items()]})
        outs = []
        for rel in _l(d.get("outputs")):
            st, swhy = _state(run_dir, str(rel), _m(rec.get("outputs")).get(rel))
            header = None
            if st in ("match", "changed") and str(rel).endswith(".csv"):
                header, _, hwhy = _rows(run_dir, str(rel))
            outs.append({"path": str(rel), "state": st, "why": swhy, "columns": header or []})
        script = d.get("script")
        s_state, s_why = _state(run_dir, str(script), _m(rec.get("script")).get("sha256")) if script else ("missing", None)
        rows.append({"id": d.get("id"), "script": script, "script_state": s_state, "script_why": s_why,
                     "inputs": ins, "outputs": outs,
                     "record": na(rwhy) if rwhy else na(f"{RC.DERIV_RECORD} has no entry {d.get('id')}") if not rec
                     else {"available": True, "ok": rec.get("ok"), "runs": rec.get("runs"),
                           "runtime_s": rec.get("runtime_s"), "python": rec.get("python"),
                           "sandbox": _m(rec.get("sandbox")).get("kind"), "libraries": _m(rec.get("libraries")),
                           "reason": rec.get("reason")}})
    return {"available": True, "file": DERIVATIONS, "rows": rows,
            "ignored_inputs": [{"path": str(k), "reason": _short(v)} for k, v in ignored.items()]}


def _input_checks(run_dir: str) -> dict:
    doc, why = _need(run_dir, INPUT_CHECKS, [("status", str), ("counts", dict), ("findings", list)])
    if why:
        return na(why)
    return {"available": True, "file": INPUT_CHECKS, "status": doc.get("status"), "reason": doc.get("reason"),
            "counts": {str(k): v for k, v in _m(doc.get("counts")).items()},
            "checks": {str(k): _short(v) for k, v in _m(doc.get("checks")).items()},
            "findings": [{"id": f.get("id"), "code": f.get("code"), "severity": f.get("severity"),
                          "units": _l(f.get("units")), "claim": f.get("claim"),
                          "accepted": f.get("accepted")} for f in _l(doc.get("findings")) if isinstance(f, dict)],
            "acceptance_problems": [_short(p) for p in _l(doc.get("acceptance_problems"))]}


def evidence_tab(run_dir: str) -> dict:
    spec, _ = _mapping(run_dir, RC.SPEC)
    flat = _flatten(spec, "", {}) if isinstance(spec, dict) else {}
    cit = _citations(run_dir)
    by_id = {c["id"]: c for c in cit.get("entries") or []}
    reg = _registry(run_dir, by_id)
    params = _param_registries(run_dir, flat, by_id)
    for c in by_id.values():
        c["cited_by"] = sorted({e["name"] for r in params for e in r.get("entries") or []
                                if c["id"] in _cited_ids(e.get("source"))} |
                               {str(e["id"]) for e in reg.get("entries") or [] if str(e.get("citation")) == c["id"]})
    dd, dwhy = _need(run_dir, DERIVATIONS, [("derivations", list)])
    ders = [d for d in _l(_m(dd).get("derivations")) if isinstance(d, dict)]
    ignored = {str(k): _short(v) for k, v in _m(_m(dd).get("ignored_inputs")).items()}
    return {"registry": reg, "param_registries": params, "citations": cit,
            "inputs": _inputs(run_dir, ders, ignored), "derivations": _derivations(run_dir, ders, ignored, dwhy),
            "input_checks": _input_checks(run_dir)}


# ------------------------------------------------------------------ Engine
def _choice(run_dir: str) -> dict:
    doc, why = _need(run_dir, CHOICE, [("engine.ref", str), ("candidates", list)])
    if why:
        return na(why)
    return {"available": True, "file": CHOICE, "at": doc.get("at"), "chosen": _m(doc.get("engine")).get("ref"),
            "why": doc.get("why"),
            "candidates": [{"ref": c.get("ref"), "name": c.get("name"), "version": c.get("version"),
                            "eligible": c.get("eligible"), "why_not": [_short(x) for x in _l(c.get("why_not"))],
                            "mechanisms": len(_l(c.get("mechanisms"))), "benchmarks": c.get("benchmarks")}
                           for c in _l(doc.get("candidates")) if isinstance(c, dict)]}


def _attr_binding(name: str, spec: dict) -> str:
    du, cal = _m(spec.get("decision_units")), _m(spec.get("calibration"))
    ea, da = _m(du.get("engine_attributes")), _m(du.get("defaults_accepted"))
    if name in ea:
        b = _binding_of(ea[name])
        return f"{b['how']} {b['detail']}" + (f" (source {b['source']})" if b.get("source") else "")
    if name in da:
        return f"default accepted: {_short(da[name])}"
    if cal.get("attribute") == name:
        return f"calibrated ({cal.get('method')})"
    return "not bound (the engine's default)"


def _restamp(eng: dict, repo: str) -> dict:
    keys = [(k, m.group(1)) for k in _m(eng.get("inputs")) for m in [_RESULTS_KEY.match(str(k))] if m]
    if not keys:
        return na(f"{ENGINE_YAML} records no engine benchmark results file in its inputs")
    key, name = keys[0]
    want, rel = eng["inputs"][key], f"toolkit/engines/{name}/benchmarks/results.yaml"
    try:
        data, _ = safe_io.read_nofollow(os.path.realpath(repo), rel, RC._MAX_READ)
    except FileNotFoundError:
        return na(f"this checkout has no {rel}")
    except (OSError, ValueError) as e:
        return na(f"{rel} in this checkout: not read ({e})")
    sha = hashlib.sha256(data).hexdigest()
    if sha != want:
        return na(f"this checkout's {rel} differs from the one the run used (sha256 {sha[:12]} here, "
                  f"{str(want)[:12]} in the run): its re-stamp status is not shown")
    try:
        doc = yaml.load(data.decode("utf-8"), Loader=RC._LOADER) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as e:
        return na(f"{rel}: {type(e).__name__}")
    ident = _m(doc.get("engine_identity"))
    rs = doc.get("restamped")
    base = {"available": True, "file": rel, "sha256": sha, "new_version": ident.get("version") or doc.get("engine_version"),
            "code_sha256": ident.get("code_sha256")}
    if not isinstance(rs, dict):
        return {**base, "state": "recomputed", "by": doc.get("producer"), "validate_once": "not needed"}
    conf = _m(rs.get("confirmation"))
    # restamp_proof.py keeps the earlier records under `previous`, newest first: the oldest record's old version
    # computed the numbers; every later version only re-stamped them.
    chain = [rs] + [r for r in _l(rs.get("previous")) if isinstance(r, dict)]
    olds = [r.get("old_engine_version") for r in chain]
    after = [str(v) for v in list(reversed(olds[:-1])) + [base["new_version"]]]
    return {**base, "state": "proof" if all("not recomputed" in str(r.get("by") or "") for r in chain) else "restamped",
            "by": rs.get("by"), "old_version": rs.get("old_engine_version"), "proof_identical": rs.get("proof_identical"),
            "computed_by": None if olds[-1] is None else str(olds[-1]), "restamped_to": after,
            "validate_once": conf.get("validate_once") or "not recorded", "evidence": conf.get("evidence"),
            "note": rs.get("note")}


def engine_tab(run_dir: str, repo: str = REPO) -> dict:
    spec, _ = _mapping(run_dir, RC.SPEC)
    spec = spec if isinstance(spec, dict) else {}
    eng, why = _need(run_dir, ENGINE_YAML, [("data.card", dict), ("data.card.name", str),
                                            ("data.card.benchmarks", list), ("data.parameters", list)])
    data = _m(_m(eng).get("data"))
    card = data.get("card")
    if why is None and not isinstance(card, dict):
        why = f"{ENGINE_YAML}: no data.card"
    out = {"choice": _choice(run_dir)}
    if why:
        out.update(card=na(why), parameters=na(why), benchmarks=na(why), restamp=na(why))
    else:
        valid = _m(card.get("valid_ranges"))
        out["card"] = {
            "available": True, "file": ENGINE_YAML, "name": card.get("name"), "version": card.get("version"),
            "mechanisms": [_short(m) for m in _l(card.get("mechanisms"))],
            "components": [{"name": str(k), "levels": _l(v)} for k, v in _m(card.get("components")).items()],
            "attributes": [{"name": str(k), "desc": _short(v), "range": valid.get(k), "binding": _attr_binding(str(k), spec)}
                           for k, v in _m(card.get("unit_attributes")).items()],
            "outputs": {str(k): _l(v) for k, v in _m(card.get("outputs")).items()},
            "age_groups": _l(card.get("age_groups")), "notes": [_short(n) for n in _l(card.get("notes"))]}
        fitted = _m(card.get("fitted"))
        out["parameters"] = {"available": True, "file": ENGINE_YAML, "rows": [
            {"name": p.get("name"), "kind": p.get("kind"), "value": p.get("value"), "units": p.get("units"),
             "lo": p.get("lo"), "hi": p.get("hi"), "box": _m(fitted.get(p.get("name"))).get("box"),
             "fitted_to": _l(_m(fitted.get(p.get("name"))).get("benchmarks")), "verified": p.get("verified"),
             "source": p.get("source")} for p in _l(data.get("parameters")) if isinstance(p, dict)]}
        rows = [{"id": b.get("id"), "metric": b.get("metric"), "role": b.get("role"), "target": b.get("target"),
                 "band": b.get("band"), "computed": b.get("computed"), "status": b.get("status"),
                 "in_band": b.get("in_band"), "source": b.get("source"),
                 "compare": b.get("compare"), "reference": b.get("reference"), "tolerance": b.get("tolerance"),
                 "error_abs": b.get("error_abs"), "error_rel": b.get("error_rel"),
                 "kind": "compare" if b.get("compare") is not None else "band",
                 "flag": b.get("status") != "in_band"} for b in _l(card.get("benchmarks")) if isinstance(b, dict)]
        out["benchmarks"] = {"available": True, "file": ENGINE_YAML, "rows": rows, "total": len(rows),
                             "in_band": sum(r["status"] == "in_band" for r in rows),
                             "out_of_band": sum(r["status"] == "out_of_band" for r in rows),
                             "held_out": sum(r["role"] == "held_out" for r in rows),
                             "held_out_in_band": sum(r["role"] == "held_out" and r["status"] == "in_band" for r in rows)}
        out["restamp"] = _restamp(eng, repo)
    e = _m(_m(eng).get("engine"))
    fps = []
    for rel in _walk(run_dir, "analysis", 4):
        if rel.endswith(".summary.json"):
            s, _ = _mapping(run_dir, rel)
            if s and s.get("code_fp") and s["code_fp"] not in fps:
                fps.append(str(s["code_fp"]))
    rs = out.get("restamp") or {}
    out["identity"] = na(why) if why and not fps else {
        "available": True, "name": e.get("name"), "version": e.get("version"), "card_sha256": e.get("card_sha256"),
        "engine_choice_sha256": _m(eng).get("engine_choice_sha256"), "code_fp": fps,
        "code_sha256": rs.get("code_sha256") if rs.get("available") else None}
    return out


# ------------------------------------------------------------------ Calibration
CAL_METHODS = ("emulator_inversion", "likelihood", "posterior", "none")


def _cal_inputs(run_dir: str, doc: dict, spec_sha, units_rel) -> tuple:
    """(why the calibration may not be joined to the current spec and units table, or None; {rel: entry read}). Each
    hash is compared with the bytes read now, and the entries are returned so that the units table is parsed from
    exactly the bytes that were hashed (_cal_units)."""
    if not doc.get("spec_sha256"):
        return f"{CALIBRATION} records no spec sha256, so its inputs cannot be checked", {}
    changed = [] if doc.get("spec_sha256") == spec_sha else [RC.SPEC]
    inputs = doc.get("inputs")
    if not isinstance(inputs, dict):
        return f"{CALIBRATION} records no input hashes, so its inputs cannot be checked", {}
    if units_rel and units_rel not in inputs:
        return f"{CALIBRATION} does not record the units table {units_rel} among its inputs", {}
    read = {}
    for rel, want in inputs.items():
        e = read[str(rel)] = _entry(run_dir, str(rel))
        if e is None or e["sha256"] != want:
            changed.append(str(rel))
    if changed:
        return (f"inputs changed since the analysis ({', '.join(changed)}): {CALIBRATION} was computed from other "
                "inputs, so its fit is not joined to the current ones"), {}
    return None, read


def _cal_units(e, units_rel, idc) -> tuple:
    """{unit id: row} of the units table from the entry whose hash was checked, and why not."""
    if e is None or e["err"] is not None or not isinstance(e["parsed"], str):
        return {}, f"{units_rel}: not read" if units_rel else "the spec names no units table"
    rows = list(csv.DictReader(io.StringIO(e["parsed"])))
    return {str(r.get(idc)): r for r in rows}, None


def calibration_tab(run_dir: str) -> dict:
    se = _entry(run_dir, RC.SPEC)
    spec = se["parsed"] if se and se["err"] is None and isinstance(se["parsed"], dict) else {}
    scal = _m(spec.get("calibration"))
    settings = {k: scal.get(k) for k in ("method", "attribute", "scale", "range", "target_column", "window", "metric")}
    doc, why = _need(run_dir, CALIBRATION, [("data", dict), ("data.method", str)])
    if why:
        return {**na(why), "settings": settings}
    data, method = doc["data"], doc["data"]["method"]
    if method not in CAL_METHODS:
        return {**na(f"{CALIBRATION}: calibration method {method!r} is not supported by the dossier"),
                "settings": settings}
    du = _m(spec.get("decision_units"))
    why, read = _cal_inputs(run_dir, doc, se["sha256"] if se else None, du.get("csv"))   # every method, first
    if why:
        return {**na(why), "settings": settings}
    if method == "none":
        return {"available": True, "file": CALIBRATION, "method": "none", "settings": settings, "needs_owner": False,
                "units": [], "regions": []}
    why = _invalid(data, CALIBRATION, [("units", dict), ("diagnostics", dict)] +
                   ([("diagnostics.converged", bool)] if method != "emulator_inversion" else []))
    if why:
        return {**na(why), "settings": settings}
    idc, regc, tcol = du.get("id"), du.get("region"), scal.get("target_column")
    table, twhy = _cal_units(read.get(du.get("csv")), du.get("csv"), idc)
    units = []
    for uid, u in data["units"].items():
        u, r = _m(u), table.get(str(uid)) or {}
        try:
            target = float(r.get(tcol)) if method == "emulator_inversion" and tcol and r.get(tcol) not in (None, "") \
                else None
        except ValueError:
            target = None
        units.append({"id": str(uid), "region": r.get(regc), "target": target, "z": u.get("z"),
                      "slope": u.get("slope"), "clamped": bool(u.get("clamped"))})
    regions: dict = {}
    for u in units:
        g = regions.setdefault(str(u["region"]), {"region": str(u["region"]), "n": 0, "targets": [], "z": [],
                                                  "clamped": 0, "slope_min": None})
        g["n"] += 1
        g["clamped"] += u["clamped"]
        if u["target"] is not None:
            g["targets"].append(u["target"])
        if _num(u["z"]) is not None:
            g["z"].append(u["z"])
        if _num(u["slope"]) is not None:
            g["slope_min"] = u["slope"] if g["slope_min"] is None else min(g["slope_min"], u["slope"])
    reg_rows = [{"region": g["region"], "n": g["n"], "clamped": g["clamped"], "slope_min": g["slope_min"],
                 "target_mean": sum(g["targets"]) / len(g["targets"]) if g["targets"] else None,
                 "z_min": min(g["z"]) if g["z"] else None, "z_max": max(g["z"]) if g["z"] else None}
                for g in sorted(regions.values(), key=lambda g: g["region"])]
    diag = data["diagnostics"]
    ident = _m(data.get("identifiability"))
    em, ewhy = _need(run_dir, EMULATOR, [("data.accuracy", dict), ("data.accuracy.holdout_points", (int, float))])
    acc = _m(_m(_m(em).get("data")).get("accuracy"))
    out = {"available": True, "file": CALIBRATION, "settings": settings, "method": method,
           "target_uncertainty": {k: _short(v) for k, v in _m(scal.get("target_uncertainty")).items()},
           "units": units, "regions": reg_rows, "join_why": twhy,
           "diagnostics": {str(k): v for k, v in diag.items() if k != "fit"},
           "identifiability": {str(k): v for k, v in ident.items() if k not in ("parameters", "classes")},
           "emulator": na(ewhy) if ewhy else {
               "available": True, "file": EMULATOR, **{k: acc.get(k) for k in (
                   "holdout_points", "training_points", "cal_logit_rmse", "reduction_r2_median", "reduction_r2_p10",
                   "reduction_mae_median", "base_log_rmse")},
               "error_model": {str(k): v for k, v in _m(acc.get("error_model")).items()},
               "features": _l(_m(_m(em).get("data")).get("features")),
               "fit_seconds": _m(_m(em).get("data")).get("fit_seconds")}}
    well = ident.get("status") == "well_identified"
    if method == "emulator_inversion":
        res = diag.get("target_logit_residual_max")
        clamped = diag.get("clamped_units") if _num(diag.get("clamped_units")) is not None else \
            sum(u["clamped"] for u in units)
        out["model_per_unit"] = ("not stored: analysis/calibration.yaml keeps each unit's calibrated value (z), slope "
                                 "and clamp, not the model's value at the fitted point"
                                 + (f"; the inversion's largest residual is {res:.2g} on the logit scale"
                                    if _num(res) is not None else ""))
        out["needs_owner"] = bool(clamped) or not well
        return out
    fit = []
    for r in _l(diag.get("fit")):
        if not isinstance(r, dict):
            continue
        o, e = _num(r.get("observed")), _num(r.get("expected"))
        fit.append({"unit": r.get("unit"), "t0": r.get("t0"), "package": r.get("package"), "observed": o,
                    "expected": e, "pearson": (o - e) / e ** 0.5 if o is not None and e and e > 0 else None})
    params = ([{"name": str(k), "status": _m(v).get("status"), "estimate": _m(v).get("estimate"),
                "se": _m(v).get("se"), "profile_ci": _m(v).get("profile_ci"), "explanation": _m(v).get("explanation")}
               for k, v in _m(ident.get("parameters")).items()] +
              [{"name": str(k), "status": v} for k, v in _m(ident.get("classes")).items()])
    out.update(fit=fit, parameters=params, theta={str(k): v for k, v in _m(data.get("theta")).items()},
               prior_dominated=_l(data.get("prior_dominated")),
               needs_owner=(diag.get("converged") is not True or bool(_l(diag.get("at_bound")))
                            or bool(diag.get("unsupported_targets")) or not well))
    return out


def dossier(run_dir: str, repo: str = REPO) -> dict:
    """{spec, evidence, engine, calibration}; a builder that fails is shown as its error, not hidden."""
    out = {}
    for name, fn in (("spec", spec_tab), ("evidence", evidence_tab), ("engine", lambda d: engine_tab(d, repo)),
                     ("calibration", calibration_tab)):
        try:
            out[name] = fn(run_dir)
        except Exception as e:  # noqa: BLE001 -- a reader bug is shown on the page
            out[name] = na(f"the {name} tab could not be built: {type(e).__name__}: {str(e)[:300]}")
    return out


# ------------------------------------------------------------------ self-test
def _toy_w(d: str, rel: str, data) -> str:
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if isinstance(data, (dict, list)):
        data = json.dumps(data, indent=1) if rel.endswith(".json") else yaml.safe_dump(data, sort_keys=False)
    with open(p, "w" if isinstance(data, str) else "wb") as f:
        f.write(data)
    return p


def _sha_file(p: str) -> str:
    import hashlib
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _toy_spec(horizon_stop: float = 2028.0, tests=("DT1", "DT2"), note_hostile: str = "") -> dict:
    """A toy decision spec (generic names, toy numbers; no data from any real source)."""
    dts = {"DT1": {"id": "DT1", "type": "parameter_value", "parameter": "p_half", "values": [100.0],
                   "finding": "benchmark_drift:B2", "text": "Does the plan change at a shorter half-life?"},
           "DT2": {"id": "DT2", "type": "interaction_rescale", "components": ["c"], "with_any": ["a", "b"],
                   "benchmark": "B1", "values": [2.0, 1.5], "text": "Does the plan change at the trial interaction?"},
           "DT3": {"id": "DT3", "type": "group_rule", "add": {"id": "same_g", "components": ["a", "b"], "by": "region",
                                                              "rule": "same_choice", "reason": "one choice per region"},
                   "text": "Does the plan change when a region must make one choice?"}}
    return {
        "schema_version": 1,
        "question": "Toy question: choose one package per unit within a budget." + note_hostile,
        "decision_maker": "Toy programme manager", "decision_date": 2024,
        "engine": {"auto": True, "candidates": ["toolkit.engines.contract:ToyEngine"]},
        "horizon": {"start": 2016.0, "stop": horizon_stop},
        "decision_units": {
            "csv": "data/derived/units.csv", "id": "unit_id", "label": "unit_id", "region": "region",
            "engine_attributes": {"season": {"columns": ["s1", "s2"]}, "m0_hint": {"column": "pop"},
                                  "use": {"value": 0.5, "source": "data/inputs/toy/params_registry.yaml use_a"},
                                  "years": {"column": "years", "split": " "}},
            "defaults_accepted": {"month": "no month schedule staged; the engine's default is used"},
            "populations": {"all": {"column": "pop", "age_groups": ["u5", "5p"]}}},
        "options": {
            "populations": {"total": "pop", "elig": "elig_pop"},
            "components": {"a": {"group": "g"}, "b": {"group": "g"}, "c": {"eligible": "elig"}, "d": {}},
            "packages": {"generate": True},
            "engine_map": {"a": {"g": "a"}, "b": {"g": "b"}, "c": {"c": 1}, "d": {"d": 1}},
            "prices": {"base_year": "2024", "index_name": "toy index", "index": {"2020": 100.0, "2024": 125.0}},
            "cost_items": [
                {"id": "a_commodity", "component": "a", "value": 2.0, "price_year": "2020", "price_rule": "index",
                 "basis": "per_commodity", "people_per_commodity": 2.0, "includes": ["commodity", "delivery"]},
                {"id": "b_commodity", "component": "b", "value": 3.0, "price_year": "2024",
                 "price_rule": "as_published", "basis": "per_commodity", "people_per_commodity": 2.0,
                 "includes": ["commodity"]},
                {"id": "c_all", "component": "c", "value": 3.0, "price_year": "2024", "price_rule": "as_published",
                 "basis": "per_eligible", "years": 3, "includes": ["drug", "delivery"]},
                {"id": "d_case", "component": "d", "value": 10.0, "price_year": "2024", "price_rule": "as_published",
                 "basis": "per_outcome", "outcome": "treated", "incremental": True, "includes": ["treatment"]}]},
        "objective": {"outcome": "dalys", "sense": "avert", "horizon": [2024.0, horizon_stop],
                      "delivery": [2024.0, 2027.0], "discounting": "none"},
        "constraints": {"envelope_usd": 1000000, "fixed_costs_usd": {"d_floor": 100000.0},
                        "eligibility": {"c": {"column": "elig"}}},
        "comparator": {"package": "none", "text": "no programme in the window"},
        "calibration": {"method": "emulator_inversion", "attribute": "m0", "scale": "log", "range": [-1.0, 2.0],
                        "target_column": "target", "window": [2021.0, 2022.0], "metric": "prev",
                        "target_uncertainty": {"region_sd_logit": 0.15, "note": "toy common-mode error" + note_hostile},
                        "target_frame": {"band": "prev", "source": "toy survey frame"}},
        "uncertainty": {"level": 0.95, "draws": 8, "seed": 1,
                        "sources": [{"kind": "priors"}, {"kind": "costs", "items": {"c_all": [0.8, 1.2]}}],
                        "excluded": []},
        "analysis": {"evaluation": "emulator", "emulator": {"points": 16, "holdout_share": 0.1, "seed": 7},
                     "decision_tests": [dts[t] for t in tests]},
        "deliverable": {"kind": "package_per_unit", "allocation_file": "allocation.csv"},
    }


UNITS_CSV = ("unit_id,region,pop,elig,elig_pop,target,s1,s2,years\n"
             "u1,R1,100,1,20,0.10,0.5,1.5,2017 2020\nu2,R1,200,1,40,0.20,0.5,1.5,2017 2020\n"
             "u3,R1,300,0,0,0.30,0.5,1.5,2017 2020\nu4,R2,400,1,80,0.40,1.0,1.0,2018\n"
             "u5,R2,500,0,0,0.50,1.0,1.0,2018\nu6,R2,600,1,120,0.60,1.0,1.0,2018\n")


def _toy_dossier(d: str, repo: str, hostile: str = "") -> str:
    """A synthetic run dir with every dossier artifact, and a toy checkout (`repo`) with the engine's results file."""
    from types import SimpleNamespace as NS  # noqa: F401
    RC._toy_run(d, state="ANALYZE", paused=None)
    h = hostile
    _toy_w(d, "data/inputs/toy/units_raw.csv", "id,region,pop,notes\nu1,R1,100,x\n")
    _toy_w(d, "data/inputs/toy/changed.csv", "now,different\n")
    _toy_w(d, "data/inputs/toy/params_registry.yaml", {"parameters": [
        {"name": "cost_a", "value": 2.0, "ci_low": 1.8, "ci_high": 2.2, "kind": "cost_usd", "source": "T1",
         "subgroup": "toy list price" + h},
        {"name": "use_a", "value": 0.5, "kind": "share", "source": "T2"}]})
    _toy_w(d, "data/inputs/toy/citations_t.txt", "# toy citations\n\n## [T1] Toy price list" + h + "\n- **Value**: 2.0 per "
       "item\n- **Source**: Toy 2020\n- **DOI/PMCID**: 10.0000/toy\n\n## [T2] Toy use survey\n- **Value**: 50%\n"
       "- **Source**: Toy survey 2021\n")
    _toy_w(d, "data/derive/units.py", "# toy derivation script\n")
    units = _toy_w(d, "data/derived/units.csv", UNITS_CSV)
    raw = os.path.join(d, "data/inputs/toy/units_raw.csv")
    _toy_w(d, "data/inputs_provenance.json", {"inputs": {
        "data/inputs/toy/units_raw.csv": {"source": "toy/units_raw.csv", "sha256": _sha_file(raw), "bytes": 40},
        "data/inputs/toy/params_registry.yaml": {"source": "toy/params_registry.yaml",
                                                 "sha256": _sha_file(os.path.join(d, "data/inputs/toy/params_registry.yaml")),
                                                 "bytes": 200},
        "data/inputs/toy/changed.csv": {"source": "toy/changed.csv", "sha256": "0" * 64, "bytes": 12},
        "data/inputs/toy/old.csv": {"source": "toy/old.csv", "sha256": "1" * 64, "bytes": 9}},
        "cited": {"data/derived/units.csv": {"sha256": _sha_file(units)}}})
    _toy_w(d, "data/derivations.yaml", {"derivations": [{
        "id": "units", "script": "data/derive/units.py", "inputs": ["data/inputs/toy/units_raw.csv"],
        "outputs": ["data/derived/units.csv"],
        "columns": {"uses": {"data/inputs/toy/units_raw.csv": ["id", "region", "pop"]},
                    "ignores": {"data/inputs/toy/units_raw.csv": {"notes": "not used: removing it leaves the units "
                                                                           "table unchanged" + h}}}}],
        "ignored_inputs": {"data/inputs/toy/changed.csv": "not read by the units derivation"}})
    rec = _toy_w(d, "data/derivations_record.json", {"producer": "scripts/pipeline.py derive", "version": 1, "derivations": {
        "units": {"id": "units", "ok": True, "runs": 3, "runtime_s": 0.5, "python": "3.14.3",
                  "sandbox": {"kind": "bwrap", "limits": {"wall_s": 900}}, "libraries": {"pandas": "3.0.2"},
                  "script": {"path": "data/derive/units.py", "sha256": _sha_file(os.path.join(d, "data/derive/units.py"))},
                  "inputs": {"data/inputs/toy/units_raw.csv": _sha_file(raw)},
                  "outputs": {"data/derived/units.csv": _sha_file(units)}, "reason": ""}}})
    _toy_w(d, "data/input_checks.json", {"producer": "toolkit/spec/geometry.py", "version": 2, "status": "ran",
                                     "reason": None, "counts": {"units": 6, "points_checked": 6, "reciprocal_swaps": 1},
                                     "checks": {"point": "ran"}, "findings": [
                                         {"id": "reciprocal_swap:u1+u2", "code": "reciprocal_swap", "severity": "HIGH",
                                          "units": ["u1", "u2"], "claim": "units u1 and u2 look exchanged" + h,
                                          "accepted": None}], "acceptance_problems": []})
    _toy_w(d, "evidence/registry.yaml", {"status": "checked", "entries": [
        {"id": "p_half", "value": 120, "units": "days", "source": "Toy trial" + h, "citation": "T1",
         "used_in": ["engine.p_half"], "status": "matched"}]})
    # sign-off history: two archived sign-offs and the current one
    specs = [_toy_spec(2027.0, ("DT1",)), _toy_spec(2028.0, ("DT1",)), _toy_spec(2028.0, ("DT1", "DT2"), h)]
    s1 = _toy_w(d, "evidence_planner/superseded_signoff_2026-01-01/decision_spec.signed_aa.yaml", specs[0])
    s2 = _toy_w(d, "evidence_planner/superseded_signoff_2026-01-02/decision_brief.signed/decision_spec.yaml", specs[1])
    cur = _toy_w(d, "decision_spec.yaml", specs[2])
    for folder, p, at, note in (("superseded_signoff_2026-01-01", s1, "2026-01-01T10:00:00+00:00", "first sign-off"),
                                ("superseded_signoff_2026-01-02", s2, "2026-01-02T10:00:00+00:00", "window extended")):
        _toy_w(d, f"evidence_planner/{folder}/decision_spec.signoff.json", {
            "signed_by": "owner", "signed_at": at, "note": note,
            "sha256": {"decision_spec.yaml": _sha_file(p), "data/derivations_record.json": _sha_file(rec)},
            "producer": "toolkit/spec/signoff.py", "schema_version": 1})
    _toy_w(d, "decision_spec.signoff.json", {"signed_by": "owner", "signed_at": "2026-01-03T10:00:00+00:00",
                                         "note": "test DT2 added" + h,
                                         "sha256": {"decision_spec.yaml": _sha_file(cur),
                                                    "data/derivations_record.json": _sha_file(rec)},
                                         "producer": "toolkit/spec/signoff.py", "schema_version": 1})
    # engine: choice, the harness's card snapshot, a toy checkout's results file re-stamped by proof
    res = _toy_w(repo, "toolkit/engines/toy/benchmarks/results.yaml", {
        "producer": "toolkit/engines/benchmark.py",
        "engine_identity": {"name": "toy", "version": "0.2", "code_sha256": "c0de" * 16},
        "benchmarks": [{"id": "B1"}, {"id": "B2"}],
        "restamped": {"by": "toolkit/engines/toy/benchmarks/restamp_proof.py (byte-identity proof, not recomputed)",
                      "proof_identical": True, "old_engine_version": "0.1",
                      "confirmation": {"validate_once": "pending", "evidence": None}}})
    _toy_w(d, "engine_choice.json", {"at": "2026-01-03T11:00:00+00:00", "engine": {"ref": "toolkit.engines.toy:Toy",
                                                                               "config": {}},
                                 "why": "the only eligible engine" + h, "candidates": [
        {"ref": "toolkit.engines.contract:ToyEngine", "name": "closed", "version": "1", "eligible": False,
         "why_not": ["no component c", "no window metric prev"], "mechanisms": ["closed-form burden"], "benchmarks": 0},
        {"ref": "toolkit.engines.toy:Toy", "name": "toy", "version": "0.2", "eligible": True, "why_not": [],
         "mechanisms": ["toy transmission", "toy interventions"], "benchmarks": 2}]})
    eng = {"name": "toy", "version": "0.2", "card_sha256": "ca7d" * 16}
    _toy_w(d, "analysis/engine.yaml", {
        "schema": "analysis.engine/1", "produced_by": "harness", "producer": "toolkit/analysis/run.py",
        "spec_sha256": _sha_file(cur), "spec_signed": True, "engine": eng, "engine_choice_sha256": "c401" * 16,
        "inputs": {"decision_spec.yaml": _sha_file(cur), "../../toolkit/engines/toy/benchmarks/results.yaml": _sha_file(res)},
        "data": {"card": {
            "name": "toy", "version": "0.2", "mechanisms": ["toy transmission" + h, "toy interventions"],
            "components": {"g": ["none", "a", "b"], "c": [0, 1], "d": [0, 1]},
            "unit_attributes": {"m0": "density per person, [0.1, 10]", "season": "12 monthly values",
                                "use": "use after a campaign [0, 1]", "month": "optional month"},
            "valid_ranges": {"m0": [0.1, 10.0]},
            "outputs": {"per_capita": ["dalys", "treated"], "windows": ["prev"], "cost_drivers": ["given"]},
            "age_groups": ["u5", "5p"],
            "fitted": {"k": {"value": 0.4, "lo": 0.3, "hi": 0.5, "box": [0.0, 1.0], "benchmarks": ["B1"]}},
            "benchmarks": [
                {"id": "B1", "metric": "toy ratio at 12 m", "role": "fit", "target": 0.8, "band": [0.7, 0.9],
                 "computed": 0.81, "in_band": True, "status": "in_band", "source": "T1"},
                {"id": "B2", "metric": "toy incidence ratio" + h, "role": "held_out", "target": 0.75, "band": [0.6, 0.9],
                 "computed": 0.5, "in_band": False, "status": "out_of_band", "source": "T2"}],
            "notes": ["toy note: an unverified assumption"]},
            "parameters": [
                {"name": "k", "value": 0.4, "kind": "fitted", "units": "probability", "lo": 0.3, "hi": 0.5,
                 "source": "fitted once to B1", "verified": "unverified"},
                {"name": "p_half", "value": 120.0, "kind": "fixed", "units": "days", "lo": 90.0, "hi": 150.0,
                 "source": "Toy trial" + h, "verified": "matched"}]}})
    _toy_w(d, "analysis/batch/points.jsonl.summary.json", {"n": 4, "computed": 4, "code_fp": "fp0123456789"})
    # calibration and emulator
    units_cal = {f"u{i}": {"z": 0.1 * i, "clamped": i == 6, "slope": 1.0 / i} for i in range(1, 7)}
    _toy_w(d, "analysis/calibration.yaml", {"schema": "analysis.calibration/1", "producer": "toolkit/analysis/calibration.py",
                                        "engine": eng, "spec_sha256": _sha_file(cur),
                                        "inputs": {"data/derived/units.csv": _sha_file(units)}, "data": {
        "method": "emulator_inversion", "units": units_cal,
        "diagnostics": {"units": 6, "clamped_units": 1, "grid_points": 10, "range": [-1.0, 2.0],
                        "target_logit_residual_max": 0.0001, "slope_median": 0.3, "slope_min": 1.0 / 6},
        "identifiability": {"design": "one attribute per unit, one target per unit (exactly determined)",
                            "flat_slope_threshold": 0.05, "units_unidentified": [], "status": "well_identified"}}})
    _toy_w(d, "analysis/emulator.yaml", {"schema": "analysis.emulator/1", "producer": "toolkit/analysis/model.py",
                                     "engine": eng, "data": {
        "accuracy": {"holdout_points": 3, "training_points": 13, "cal_logit_rmse": 0.12, "reduction_r2_median": 0.9,
                     "reduction_r2_p10": 0.8, "reduction_mae_median": 0.02, "base_log_rmse": 0.05,
                     "error_model": {"model": "toy common plus per-unit", "rmse_median": 0.03}},
        "features": ["s1", "pop"], "fit_seconds": 1.5}})
    return d


def _node(node: str, tail: str):
    """Run the page's two scripts in node with a stub DOM, then `tail` (the self-test renders the tabs this way)."""
    import subprocess
    stub = (
        "const _h = { get: (t, k) => k === Symbol.iterator ? function* () {} : k === Symbol.toPrimitive ? "
        "() => '' : k === 'forEach' ? () => {} : k === 'search' ? '' : k === 'content' ? 'tok' : P, "
        "set: () => true, apply: () => P };\nconst P = new Proxy(function () {}, _h);\n"
        "globalThis.document = P; globalThis.window = P; globalThis.location = { search: '' };\n"
        "globalThis.matchMedia = () => ({ matches: false }); globalThis.setInterval = () => 0; "
        "globalThis.setTimeout = () => 0;\nglobalThis.fetch = () => new Promise(() => {});\n")
    js = open(os.path.join(HERE, "run_console.js"), encoding="utf-8").read() + "\n" + open(JS, encoding="utf-8").read()
    glossary = json.dumps({k: {"text": v[0], "source": v[1]} for k, v in RC.GLOSSARY.items()})
    return subprocess.run([node, "-e", stub + js + f"\nS.meta = {{ glossary: {glossary} }};\n" + tail],
                          capture_output=True, text=True, timeout=60)


def _self_test_review1(ok, td: str) -> None:
    """DR1-DR9: Codex review round 1 of PR #132, each finding reproduced first."""
    import shutil

    def case(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            ok(False, f"{name}: {type(e).__name__}: {str(e)[:300]}")

    node = shutil.which("node")

    def html_of(dd, tab):
        if not node:
            return None
        p = _node(node, f"process.stdout.write(dossierHTML({json.dumps(tab)}, {json.dumps(dd[tab], default=str)})); "
                        "process.exit(0);\n")
        return p.stdout or ("NODE FAILED " + p.stderr[-300:])

    def fresh(name):
        return _toy_dossier(os.path.join(td, "r1", name), os.path.join(td, "r1", "repo_" + name))

    # DR1 (P1) the calibration join is refused when the units table or the spec changed after the analysis
    def dr1():
        d = fresh("dr1a")
        _toy_w(d, "data/derived/units.csv", UNITS_CSV.replace("u1,R1,100,1,20,0.10", "u1,R1,100,1,20,0.90"))
        c1 = calibration_tab(d)
        d2 = fresh("dr1b")
        sp = yaml.safe_load(open(os.path.join(d2, "decision_spec.yaml")))
        sp["calibration"]["target_column"] = "pop"
        _toy_w(d2, "decision_spec.yaml", sp)
        c2 = calibration_tab(d2)
        c3 = calibration_tab(fresh("dr1c"))
        ok(c1.get("available") is False and "changed since the analysis" in c1.get("why", "")
           and c2.get("available") is False and "changed since the analysis" in c2.get("why", "")
           and c3.get("available") is True,
           f"DR1 a units table or spec edited after the analysis is not joined to the old fit ({c1.get('why')}; "
           f"{c2.get('why')})")
    case("DR1", dr1)

    # DR2 (P1) the calibration tab renders by the artifact's method; failures need the owner
    def dr2():
        d = fresh("dr2")
        doc = yaml.safe_load(open(os.path.join(d, "analysis/calibration.yaml")))
        doc["data"] = {"method": "likelihood", "theta": {"k": 0.5}, "names": ["k", "z:u1"],
                       "units": {f"u{i}": {"z": 0.1 * i} for i in range(1, 7)},
                       "diagnostics": {"converged": False, "message": "ABNORMAL", "iterations": 9, "n_targets": 2,
                                       "n_parameters": 2, "dispersion": 1.4, "pearson_chi2": 2.8, "df": 1,
                                       "at_bound": ["k"], "pearson_residual_max": 1.9, "unsupported_targets": 0,
                                       "fit": [{"unit": "u1", "t0": 2020, "package": "none", "observed": 12.0,
                                                "expected": 9.0},
                                               {"unit": "u2", "t0": 2020, "package": "none", "observed": 4.0,
                                                "expected": 4.0}]},
                       "identifiability": {"method": "scripts/identifiability.py", "status": "weakly_or_unidentified",
                                           "parameters": {"k": {"status": "weakly_identified", "explanation": "flat",
                                                                "estimate": 0.5, "se": 0.4,
                                                                "profile_ci": [0.1, 0.9]}}}}
        _toy_w(d, "analysis/calibration.yaml", doc)
        c = calibration_tab(d)
        fit = (c.get("fit") or [{}])[0]
        ok(c.get("method") == "likelihood" and (c.get("diagnostics") or {}).get("converged") is False
           and c.get("needs_owner") is True and abs((fit.get("pearson") or 0) - 1.0) < 1e-9,
           f"DR2a a likelihood fit: convergence, bound hits, observed vs expected with the Pearson residual, and it "
           f"needs the owner ({ {k: c.get(k) for k in ('method', 'needs_owner')} }; {fit})")
        doc["data"]["method"] = "magic"
        _toy_w(d, "analysis/calibration.yaml", doc)
        cu = calibration_tab(d)
        ok(cu.get("available") is False and "not supported" in cu.get("why", ""),
           f"DR2b an unknown calibration method is not supported ({cu.get('why')})")
        if node:
            doc["data"]["method"] = "likelihood"
            _toy_w(d, "analysis/calibration.yaml", doc)
            h = html_of(dossier(d), "calibration") or ""
            hi = html_of(dossier(fresh("dr2b")), "calibration") or ""
            ok("reproduces its target" not in h and "Needs you: yes" in h and "Pearson" in h
               and "Needs you: yes" in hi,
               f"DR2c the likelihood tab has no inversion wording and says Needs you: yes; a clamped inversion does too "
               f"({h[:160]!r})")
    case("DR2", dr2)

    # DR3 (P2) an empty or mistyped payload is not available, never zeros
    def dr3():
        d = fresh("dr3")
        for rel in (REGISTRY, PROVENANCE, DERIVATIONS, INPUT_CHECKS, CHOICE, ENGINE_YAML, CALIBRATION, EMULATOR):
            _toy_w(d, rel, {} if rel.endswith(".json") else "{}\n")
        dd = dossier(d)
        ev, en, ca = dd["evidence"], dd["engine"], dd["calibration"]
        parts = {"registry": ev["registry"], "inputs": ev["inputs"], "derivations": ev["derivations"],
                 "input_checks": ev["input_checks"], "choice": en["choice"], "card": en["card"],
                 "benchmarks": en["benchmarks"], "calibration": ca, "emulator": ca.get("emulator") or ca}
        bad = {k: v.get("why") for k, v in parts.items() if v.get("available") is not False}
        ok(not bad and all("missing" in (v.get("why") or "") or "invalid" in (v.get("why") or "")
                           for v in parts.values()),
           f"DR3 a {{}} payload is not available with the missing field named, never shown as zeros "
           f"(shown as available: {sorted(bad)}; {[v.get('why') for v in parts.values()][:3]})")
    case("DR3", dr3)

    # DR4 (P2) per_reached: the cost per person reached, with the reach share
    def dr4():
        opts = {"components": {"r": {"eligible": "elig", "reach_share": 0.5}},
                "prices": {"base_year": "2024"},
                "cost_items": [{"id": "r_item", "component": "r", "value": 2.0, "price_year": "2024",
                                "price_rule": "as_published", "basis": "per_reached", "includes": ["x"]}]}
        it = _cost_items(opts)[0]
        ok(abs((it["per_person"] or 0) - 2.0) < 1e-9 and it["per_label"] == "per person reached"
           and any("reach share 0.5" in x for x in it["extra"]),
           f"DR4 a per_reached item is priced per person reached and shows its reach share ({it})")
    case("DR4", dr4)

    # DR5 (P2) an archived spec is hashed and parsed from the same read
    def dr5():
        d = fresh("dr5")
        rel = "evidence_planner/superseded_signoff_2026-01-01/decision_spec.signed_aa.yaml"
        calls, real = [], RC._load_rel

        def counting(run_dir, r):
            if r == rel:
                calls.append(r)
            return real(run_dir, r)
        RC._load_rel = counting
        try:
            so = spec_tab(d)["signoffs"]
        finally:
            RC._load_rel = real
        ok(len(calls) == 1 and so[0]["diff"]["available"],
           f"DR5 the archived spec is read once, its hash and its parsed content from that read ({len(calls)} reads)")
    case("DR5", dr5)

    # DR6 (P2) the re-stamp chain: who computed the numbers and every re-stamp since
    def dr6():
        d = fresh("dr6")
        repo = os.path.join(td, "r1", "repo_dr6")
        res = os.path.join(repo, "toolkit/engines/toy/benchmarks/results.yaml")
        doc = yaml.safe_load(open(res))
        doc["engine_identity"]["version"] = "0.8"
        doc["restamped"] = {"by": "restamp_proof.py (byte-identity proof, not recomputed)", "proof_identical": True,
                            "old_engine_version": "0.7", "confirmation": {"validate_once": "pending"},
                            "previous": [{"by": "restamp_proof.py (byte-identity proof, not recomputed)",
                                          "old_engine_version": "0.6"},
                                         {"by": "restamp_proof.py (byte-identity proof, not recomputed)",
                                          "old_engine_version": "0.5"}]}
        _toy_w(repo, "toolkit/engines/toy/benchmarks/results.yaml", doc)
        eng = yaml.safe_load(open(os.path.join(d, ENGINE_YAML)))
        k = next(x for x in eng["inputs"] if "results.yaml" in x)
        eng["inputs"][k] = _sha_file(res)
        _toy_w(d, ENGINE_YAML, eng)
        rs = engine_tab(d, repo)["restamp"]
        ok(rs.get("computed_by") == "0.5" and rs.get("restamped_to") == ["0.6", "0.7", "0.8"],
           f"DR6a the chain: computed by v0.5, re-stamped by proof to v0.6, v0.7, v0.8 "
           f"({ {k: rs.get(k) for k in ('computed_by', 'restamped_to', 'old_version')} })")
        if node:
            h = html_of(dossier(d, repo), "engine") or ""
            ok("computed by v0.5" in h and "v0.6, v0.7, v0.8" in h and "numbers from version" not in h,
               "DR6b the page says which version computed the numbers and which only re-stamped them")
    case("DR6", dr6)

    # DR7 (P2) an equation or identity benchmark keeps its criterion
    def dr7():
        d = fresh("dr7")
        eng = yaml.safe_load(open(os.path.join(d, ENGINE_YAML)))
        eng["data"]["card"]["benchmarks"].append({
            "id": "FS1a", "metric": "toy final size", "role": "held_out", "target": None, "band": None,
            "source": "closed form", "computed": 0.5828116449805854, "reference": 0.58281164498064,
            "error_abs": 5.46e-14, "error_rel": 9.37e-14, "tolerance": {"abs": 1e-6}, "compare": "eq",
            "in_band": True, "status": "in_band"})
        _toy_w(d, ENGINE_YAML, eng)
        row = next(r for r in engine_tab(d)["benchmarks"]["rows"] if r["id"] == "FS1a")
        ok(row.get("compare") == "eq" and row.get("reference") == 0.58281164498064
           and row.get("tolerance") == {"abs": 1e-6} and row.get("error_abs") == 5.46e-14,
           f"DR7a the row keeps reference, compare, tolerance and errors ({row})")
        if node:
            h = html_of(dossier(d), "engine") or ""
            ok("reference" in h and "tolerance" in h and "5.46e-14" in h,
               "DR7b the page shows the criterion of an equation benchmark, not blank target and band")
    case("DR7", dr7)

    # DR8 (P2) small values keep their significant digits
    def dr8():
        if not node:
            ok(True, "DR8 skipped: node is not installed")
            return
        p = _node(node, "process.stdout.write(JSON.stringify([sig(1.2e-8), sig(0.000072), sig(0.4512), sig(0)])); "
                        "process.exit(0);\n")
        v = json.loads(p.stdout or "[]")
        ok(len(v) == 4 and v[0] == "1.2e-8" and v[1] == "7.2e-5" and v[2] == "0.451" and v[3] == "0",
           f"DR8 sig() writes small magnitudes in scientific notation with their significant digits ({v})")
    case("DR8", dr8)

    # DR9 (P2) decision tests: a group rule removed; an envelope test in USD
    def dr9():
        spec = _toy_spec()
        spec["constraints"]["group_rules"] = [{"id": "same_region", "by": "region", "components": ["a", "b"],
                                               "rule": "same_choice", "reason": "one choice per region"}]
        spec["analysis"]["decision_tests"] = [
            {"id": "DT4", "type": "group_rule", "rule": "same_region", "text": "without the region rule?"},
            {"id": "DT5", "type": "envelope", "values": [800000, 1200000], "text": "at other envelopes?"}]
        t = {x["id"]: x for x in _decision_tests(spec, {})}
        ok("None" not in t["DT4"]["varied"] and "same_region" in t["DT4"]["varied"] and "removed" in t["DT4"]["varied"]
           and "same_choice" in t["DT4"]["varied"] and "do not define" not in t["DT4"]["varied"]
           and t["DT5"]["unit"] == "USD" and "envelope" in t["DT5"]["varied"],
           f"DR9 a removed group rule and an envelope test (USD) say what is varied ({t['DT4']['varied']!r}; "
           f"{t['DT5']})")
    case("DR9", dr9)

    # DR9b a cost_items decision test names each cost item and its multiplier
    def dr9b():
        spec = _toy_spec()
        spec["analysis"]["decision_tests"] = [
            {"id": "DT6", "type": "cost_items", "multipliers": {"ca": 0.8, "cb": 1.25}, "text": "at other prices?"}]
        t = _decision_tests(spec, {})[0]
        ok("unit costs" in t["varied"] and "ca ×0.8" in t["varied"] and "cb ×1.25" in t["varied"]
           and "{" not in t["varied"] and t["values"] == [],
           f"DR9b a cost_items test says which unit costs are multiplied, and by how much ({t['varied']!r})")
    case("DR9b", dr9b)

    # DR10 (P1, round 2) the units table is hashed and parsed from one read
    def dr10():
        d = fresh("dr10")
        calls, real = [], RC._load_rel

        def counting(run_dir, r):
            if r == "data/derived/units.csv":
                calls.append(r)
            return real(run_dir, r)
        RC._load_rel = counting
        try:
            c = calibration_tab(d)
        finally:
            RC._load_rel = real
        ok(c.get("available") is True and len(calls) == 1,
           f"DR10 the units table the calibration joins is the bytes whose hash was checked: one read ({len(calls)})")
    case("DR10", dr10)

    # DR11 (P2, round 2) method none: the freshness checks run first too
    def dr11():
        d = fresh("dr11")
        doc = yaml.safe_load(open(os.path.join(d, CALIBRATION)))
        doc["data"] = {"method": "none", "units": {}, "diagnostics": {}, "identifiability": {"status": "not_applicable"}}
        _toy_w(d, CALIBRATION, doc)
        fresh_ok = calibration_tab(d)
        _toy_w(d, "data/derived/units.csv", UNITS_CSV.replace("u1,R1,100", "u1,R1,999"))
        stale = calibration_tab(d)
        ok(fresh_ok.get("available") is True and stale.get("available") is False
           and "changed since the analysis" in stale.get("why", ""),
           f"DR11 a method-none calibration is checked against its inputs like any other ({stale.get('why')})")
    case("DR11", dr11)

    # DR12 (P2, round 2) a benchmark tolerance keeps its significant digits
    def dr12():
        if not node:
            ok(True, "DR12 skipped: node is not installed")
            return
        p = _node(node, "process.stdout.write(criterion({kind: 'compare', compare: 'eq', reference: 1, "
                        "tolerance: {abs: 1e-15}, error_abs: 0, error_rel: 0})); process.exit(0);\n")
        ok("1e-15" in p.stdout and "abs: 0;" not in p.stdout,
           f"DR12 a tolerance of 1e-15 is shown as 1e-15, not 0 ({p.stdout[:160]!r})")
    case("DR12", dr12)

    # DR13 (Codex S1-3 P3) an overlap a cost item justifies is shown beside includes, with its reason
    def dr13():
        opts = {"components": {"t": {}}, "prices": {"base_year": "2024"},
                "cost_items": [{"id": "t_case", "component": "t", "value": 1.0, "price_year": "2024",
                                "price_rule": "as_published", "basis": "per_outcome", "outcome": "treated",
                                "includes": ["programme"]},
                               {"id": "t_setup", "component": "t", "value": 9.0, "price_year": "2024",
                                "price_rule": "as_published", "basis": "per_unit", "includes": ["programme"],
                                "not_double_counted": {"t_case": "per-case staff time only; this is overhead"}}]}
        it = {x["id"]: x for x in _cost_items(opts)}
        ok(it["t_setup"].get("not_double_counted") == [{"item": "t_case",
                                                        "reason": "per-case staff time only; this is overhead"}]
           and it["t_case"].get("not_double_counted") == [],
           f"DR13a a cost item carries the overlap it justifies and the reason ({it['t_setup']})")
        if node:
            p = _node(node, "process.stdout.write(dossierHTML('spec', " + json.dumps({"cost_items": list(it.values())})
                      + ")); process.exit(0);\n")
            ok("not a double count with t_case" in p.stdout and "this is overhead" in p.stdout,
               f"DR13b the page shows the justified overlap beside includes ({(p.stdout or p.stderr)[-200:]!r})")
    case("DR13", dr13)


def _self_test() -> int:
    import builtins
    import re
    import shutil
    import subprocess
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def case(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            ok(False, f"{name}: {type(e).__name__}: {str(e)[:300]}")

    td = tempfile.mkdtemp(prefix="dossier_st_")
    try:
        repo = os.path.join(td, "repo")
        run = _toy_dossier(os.path.join(td, "runs", "r1"), repo)

        # DS1 each tab's data model on a toy run with every artifact
        def ds1():
            sp = spec_tab(run)
            u = sp["units"]
            ok(u.get("count") == 6 and u.get("regions") == 2 and u.get("id") == "unit_id" and u.get("region") == "region",
               f"DS1a units: 6 units in 2 regions from the units table the spec names ({u})")
            b = {r["attribute"]: r for r in sp["bindings"]}
            ok(b["season"]["how"] == "columns" and b["use"]["how"] == "value" and "params_registry" in b["use"]["source"]
               and b["years"]["split"] == " " and sp["defaults"][0]["name"] == "month",
               f"DS1b bindings (column, columns, value with source, split) and defaults accepted ({list(b)})")
            ci = {r["id"]: r for r in sp["cost_items"]}
            ok(abs(ci["a_commodity"]["base_value"] - 2.5) < 1e-9 and abs(ci["a_commodity"]["per_person"] - 1.25) < 1e-9
               and abs(ci["c_all"]["per_person"] - 9.0) < 1e-9 and ci["d_case"]["per_person"] is None
               and ci["a_commodity"]["includes"] == ["commodity", "delivery"],
               f"DS1c cost items: base-year value 2.5 and 1.25 per person (index rule, 2 people per item), 9.0 over "
               f"3 years per eligible, none for a per-outcome item ({ci.get('a_commodity')})")
            dt = {t["id"]: t for t in sp["decision_tests"]}
            ok(dt["DT1"]["unit"] == "days" and dt["DT1"]["values"] == [100.0] and "p_half" in dt["DT1"]["varied"]
               and dt["DT2"]["unit"] == "×" and "c" in dt["DT2"]["varied"],
               f"DS1d decision tests: units from the card's parameter table, × for an interaction ({dt})")
            ok(sp["objective"]["window"] == [2024.0, 2028.0] and sp["constraints"]["envelope_usd"] == 1000000
               and sp["constraints"]["fixed"][0]["name"] == "d_floor" and sp["comparator"]["package"] == "none"
               and sp["calibration"]["method"] == "emulator_inversion" and sp["uncertainty"]["draws"] == 8,
               "DS1e objective, constraints, comparator, calibration and uncertainty")
            so = sp["signoffs"]
            ok([s["signed_at"][:10] for s in so] == ["2026-01-01", "2026-01-02", "2026-01-03"] and so[-1]["current"]
               and so[-1]["matches"] is True and so[0]["spec_sha256"] and so[0]["note"] == "first sign-off",
               f"DS1f sign-off history in time order, the current one matching the spec ({[s.get('signed_at') for s in so]})")
            d0, d1 = so[0]["diff"], so[1]["diff"]
            f0 = {r["field"]: r for r in d0.get("rows") or []}
            f1 = {r["field"]: r for r in d1.get("rows") or []}
            ok("objective.horizon" in f0 and f0["objective.horizon"]["section"] == "objective"
               and any(k.startswith("analysis.decision_tests.DT2") and f1[k]["before"] is None for k in f1)
               and not any(k.startswith("analysis.decision_tests.DT1") for k in f1) and "diff" not in so[-1],
               f"DS1g each archived sign-off is diffed against the next: a changed field, a test added by id "
               f"({list(f0)[:4]}; {list(f1)[:4]})")
            ev = evidence_tab(run)
            reg = ev["registry"]
            ok(reg["entries"][0]["id"] == "p_half" and reg["entries"][0]["status"] == "matched" and not reg["stub"],
               f"DS1h the evidence registry's entries ({reg})")
            pr = {e["name"]: e for e in ev["param_registries"][0]["entries"]}
            ok(pr["cost_a"]["ci"] == [1.8, 2.2] and pr["cost_a"]["citation_title"].startswith("Toy price list")
               and any("engine_attributes.use" in f for f in pr["use_a"]["used_in"]),
               f"DS1i a staged parameter registry: interval, citation title, where the spec uses it ({pr})")
            cit = {c["id"]: c for c in ev["citations"]["entries"]}
            ok(cit["T1"]["fields"].get("Source") == "Toy 2020" and "cost_a" in cit["T1"]["cited_by"],
               f"DS1j citations parsed, with the entries that cite them ({cit.get('T1')})")
            st = {r["path"]: r for r in ev["inputs"]["rows"]}
            ok(st["data/inputs/toy/units_raw.csv"]["state"] == "match" and st["data/inputs/toy/changed.csv"]["state"]
               == "changed" and st["data/inputs/toy/old.csv"]["state"] == "missing"
               and st["data/inputs/toy/units_raw.csv"]["read_by"] == ["units"]
               and "not read" in st["data/inputs/toy/changed.csv"]["ignored"],
               f"DS1k staged inputs: match, changed, missing; read by a derivation or ignored with a reason ({st})")
            dv = ev["derivations"]["rows"][0]
            inp = dv["inputs"][0]
            ok(inp["uses"] == ["id", "region", "pop"] and inp["ignores"][0]["column"] == "notes"
               and "unchanged" in inp["ignores"][0]["reason"] and dv["outputs"][0]["columns"][:2] == ["unit_id", "region"]
               and dv["outputs"][0]["state"] == "match" and dv["record"]["ok"] is True and dv["script"] == "data/derive/units.py",
               f"DS1l derivation: input → script → output columns, used and ignored columns with reasons ({dv})")
            ic = ev["input_checks"]
            ok(ic["status"] == "ran" and ic["findings"][0]["severity"] == "HIGH" and ic["counts"]["reciprocal_swaps"] == 1,
               f"DS1m input checks summarised ({ic})")
            en = engine_tab(run, repo)
            ch = en["choice"]
            ok(ch["chosen"] == "toolkit.engines.toy:Toy" and [c["eligible"] for c in ch["candidates"]] == [False, True]
               and ch["candidates"][0]["why_not"] == ["no component c", "no window metric prev"],
               f"DS1n engine choice: candidates, why each was ineligible, the chosen engine ({ch})")
            at = {a["name"]: a for a in en["card"]["attributes"]}
            ok(at["m0"]["range"] == [0.1, 10.0] and at["season"]["binding"].startswith("columns")
               and at["month"]["binding"].startswith("default accepted"),
               f"DS1o card attributes with valid ranges and the spec's bindings ({at})")
            pa = {p["name"]: p for p in en["parameters"]["rows"]}
            ok(pa["k"]["kind"] == "fitted" and pa["k"]["box"] == [0.0, 1.0] and pa["k"]["fitted_to"] == ["B1"]
               and pa["p_half"]["units"] == "days" and pa["p_half"]["lo"] == 90.0,
               f"DS1p parameters: kind, prior range, fit box and benchmarks ({pa})")
            bm = en["benchmarks"]
            b2 = next(r for r in bm["rows"] if r["id"] == "B2")
            ok(bm["out_of_band"] == 1 and b2["flag"] and b2["role"] == "held_out" and b2["computed"] == 0.5,
               f"DS1q benchmarks: the out-of-band one flagged ({bm})")
            rs = en["restamp"]
            ok(rs.get("state") == "proof" and rs.get("validate_once") == "pending" and rs.get("old_version") == "0.1",
               f"DS1r re-stamp by proof, validate_once pending, from the checkout file the run recorded ({rs})")
            idn = en["identity"]
            ok(idn["version"] == "0.2" and idn["card_sha256"] == "ca7d" * 16 and "fp0123456789" in idn["code_fp"]
               and idn.get("code_sha256") == "c0de" * 16,
               f"DS1s engine identity: version, card sha256, code fingerprint ({idn})")
            ca = calibration_tab(run)
            rg = {r["region"]: r for r in ca["regions"]}
            ok(rg["R1"]["n"] == 3 and abs(rg["R1"]["target_mean"] - 0.2) < 1e-9 and rg["R2"]["clamped"] == 1
               and len(ca["units"]) == 6 and ca["diagnostics"]["clamped_units"] == 1
               and ca["identifiability"]["status"] == "well_identified" and ca["emulator"]["holdout_points"] == 3
               and "not stored" in ca["model_per_unit"],
               f"DS1t calibration: per-region rows, clamped units, identifiability, emulator held-out accuracy ({rg})")
        case("DS1", ds1)

        # DS2 fail closed: missing, unparsable, an archived spec not found, a checkout file with another hash
        def ds2():
            e = os.path.join(td, "runs", "empty")
            RC._toy_run(e, state="SPEC", paused=None)
            dd = dossier(e, repo)
            sp, ev, en, ca = dd["spec"], dd["evidence"], dd["engine"], dd["calibration"]
            parts = [sp, ev["registry"], ev["inputs"], ev["derivations"], ev["input_checks"], ev["citations"],
                     en["choice"], en["card"], en["parameters"], en["benchmarks"], en["restamp"], ca]
            ok(all(p.get("available") is False and "not written" in p.get("why", "") for p in parts),
               f"DS2a every part of a run without the artifacts is not available, with a reason "
               f"({[p.get('why') for p in parts]})")
            b = os.path.join(td, "runs", "bad")
            _toy_dossier(b, repo)
            _toy_w(b, "evidence/registry.yaml", "entries: [unclosed\n")
            _toy_w(b, "analysis/engine.yaml", "data: {card: [\n")
            os.remove(os.path.join(b, "evidence_planner/superseded_signoff_2026-01-01/decision_spec.signed_aa.yaml"))
            reg, en2 = evidence_tab(b)["registry"], engine_tab(b, repo)
            so = spec_tab(b)["signoffs"]
            ok(reg["available"] is False and "Error" in reg["why"] and en2["card"]["available"] is False
               and "Error" in en2["card"]["why"] and so[0]["diff"]["available"] is False
               and "no spec" in so[0]["diff"]["why"],
               f"DS2b an unparsable file and an archived spec not found by hash each give their reason "
               f"({reg.get('why')}; {so[0]['diff']})")
            c = os.path.join(td, "runs", "other_checkout")
            repo2 = os.path.join(td, "repo2")
            _toy_dossier(c, repo2)
            _toy_w(repo2, "toolkit/engines/toy/benchmarks/results.yaml", "producer: x\nbenchmarks: []\n")
            rs = engine_tab(c, repo2)["restamp"]
            ok(rs["available"] is False and "differs" in rs["why"],
               f"DS2c a checkout results file whose sha256 differs from the run's shows no re-stamp status ({rs})")
        case("DS2", ds2)

        # DS3 no reads outside the run dir
        def ds3():
            out = os.path.join(td, "outside")
            os.makedirs(out, exist_ok=True)
            secret = "SENTINEL-" + "7f3a9c"
            _toy_w(out, "units.csv", "unit_id,region\n" + secret + ",R\n")
            _toy_w(out, "signoff/decision_spec.signoff.json", {"signed_by": secret, "signed_at": "2026-01-01T00:00:00Z",
                                                            "note": secret, "sha256": {}})
            h = os.path.join(td, "runs", "hostile")
            _toy_dossier(h, repo)
            spec = yaml.safe_load(open(os.path.join(h, "decision_spec.yaml")))
            spec["decision_units"]["csv"] = "../../outside/units.csv"
            _toy_w(h, "decision_spec.yaml", spec)
            der = yaml.safe_load(open(os.path.join(h, "data/derivations.yaml")))
            der["derivations"][0]["inputs"] = ["/etc/hosts", "data/inputs/toy/link.csv"]
            der["derivations"][0]["outputs"] = ["../outside/units.csv"]
            _toy_w(h, "data/derivations.yaml", der)
            os.symlink(os.path.join(out, "units.csv"), os.path.join(h, "data/inputs/toy/link.csv"))
            prov = json.load(open(os.path.join(h, "data/inputs_provenance.json")))
            prov["inputs"]["../../outside/units.csv"] = {"source": "x", "sha256": "2" * 64}
            prov["inputs"]["data/inputs/toy/link.csv"] = {"source": "x", "sha256": "3" * 64}
            _toy_w(h, "data/inputs_provenance.json", prov)
            os.symlink(os.path.join(out, "signoff"), os.path.join(h, "evidence_planner", "superseded_signoff_link"))
            from lib import safe_io
            seen, real_open, real_nf, real_dfd = [], builtins.open, safe_io.open_nofollow, safe_io.dir_fd_nofollow
            real_os_open, real_scandir, real_listdir = os.open, os.scandir, os.listdir

            def t_open(p, *a, **k):
                if not isinstance(p, int):
                    seen.append(("open", os.path.realpath(p)))
                return real_open(p, *a, **k)

            def t_nf(base, rel):
                seen.append(("nofollow", os.path.realpath(base), rel))
                return real_nf(base, rel)

            def t_dfd(base, parts, create=False):
                seen.append(("dirfd", os.path.realpath(base), "/".join(parts)))
                return real_dfd(base, parts, create)

            def t_os_open(p, flags, *a, **k):
                if k.get("dir_fd") is None and not isinstance(p, int):
                    # lib.safe_io opens a trusted root by walking it from "/" (a directory fd, O_NOFOLLOW below it);
                    # that walk's start is recorded apart: it reads no file
                    walk = p == os.sep and flags & os.O_DIRECTORY
                    seen.append(("walk" if walk else "os.open", os.path.realpath(p)))
                return real_os_open(p, flags, *a, **k)

            def t_scan(p="."):
                if not isinstance(p, int):
                    seen.append(("scandir", os.path.realpath(p)))
                return real_scandir(p)

            def t_list(p="."):
                if not isinstance(p, int):
                    seen.append(("listdir", os.path.realpath(p)))
                return real_listdir(p)
            builtins.open, safe_io.open_nofollow, safe_io.dir_fd_nofollow = t_open, t_nf, t_dfd
            os.open, os.scandir, os.listdir = t_os_open, t_scan, t_list
            RC._CACHE.clear()
            try:
                dd = dossier(h, repo)
            finally:
                builtins.open, safe_io.open_nofollow, safe_io.dir_fd_nofollow = real_open, real_nf, real_dfd
                os.open, os.scandir, os.listdir = real_os_open, real_scandir, real_listdir
            blob = json.dumps(dd, default=str)
            ok(secret not in blob, "DS3a the sentinel outside the run dir never appears in the dossier")
            u = dd["spec"]["units"]
            ok(u.get("count") is None and "relative path" in (u.get("count_why") or ""),
               f"DS3b a units table named outside the run dir is refused with a reason ({u})")
            ins = {i["path"]: i for i in dd["evidence"]["derivations"]["rows"][0]["inputs"]}
            st = {r["path"]: r for r in dd["evidence"]["inputs"]["rows"]}
            ok(ins["/etc/hosts"]["state"] == "refused" and ins["data/inputs/toy/link.csv"]["state"] == "refused"
               and st["../../outside/units.csv"]["state"] == "refused" and st["data/inputs/toy/link.csv"]["state"] == "refused",
               f"DS3c derivation inputs and provenance paths outside the run dir, or links, are refused ({ins}; "
               f"{ {k: v['state'] for k, v in st.items()} })")
            root, rrepo = os.path.realpath(h), os.path.realpath(repo)
            allowed_repo = {("nofollow", rrepo, "toolkit/engines/toy/benchmarks/results.yaml")}

            def inside(p):
                return p == root or p.startswith(root + os.sep)
            stray = [s for s in seen if s[0] != "walk" and not inside(s[1]) and s not in allowed_repo
                     and not (s[0] == "dirfd" and s[1] == rrepo)
                     and not (s[0] == "os.open" and s[1] == rrepo)]
            ok(seen and not stray, f"DS3d every file the dossier opens or lists is under the run dir, or the checkout's "
                                   f"results file the run recorded ({len(seen)} traced; outside: {stray[:5]})")
        case("DS3", ds3)

        # DS4 escaping: the attribute lint covers dossier.js; hostile strings render escaped
        def ds4():
            src = open(JS, encoding="utf-8").read()
            bad = [(a, e) for a, e in RC._attr_interpolations(src) if not RC._SAFE_ATTR.match(e)
                   and not RC._LIT_TERNARY.match(e)]
            ok(not bad and "dossier.js" in RC._page_sources() + "".join(open(RC.HTML).read()),
               f"DS4a every ${{...}} in an HTML attribute of dossier.js is esc(), ref(), q() or a ternary of literals; "
               f"the page sources include it ({bad[:6]})")
            node = shutil.which("node")
            if not node:
                ok(True, "DS4b skipped: node is not installed")
                return
            hostile = '<img src=x onerror=alert(1)>" onmouseover="alert(2)'
            hr = _toy_dossier(os.path.join(td, "runs", "xss"), os.path.join(td, "repo_x"), hostile)
            dd = dossier(hr, os.path.join(td, "repo_x"))
            p = _node(node, f"const D = {json.dumps(dd, default=str)};\n"
                            "const out = {}; for (const t of ['spec', 'evidence', 'engine', 'calibration']) "
                            "out[t] = dossierHTML(t, D[t]);\nprocess.stdout.write(JSON.stringify(out)); process.exit(0);\n")
            try:
                html = json.loads(p.stdout)
            except ValueError:
                ok(False, f"DS4b node rendered the tabs ({p.stderr[-400:]})")
                return
            leaks = {t: [m for m in ("<img src=x", '" onmouseover="') if m in h] for t, h in html.items()}
            esc_seen = {t: "&lt;img src=x" in h for t, h in html.items()}
            ok(not any(leaks.values()) and all(esc_seen.values()),
               f"DS4b hostile strings in the spec, registry, card and sign-off note reach each tab's HTML only "
               f"escaped (raw: {leaks}; escaped present: {esc_seen})")
        case("DS4", ds4)

        # DS5 the endpoint, the script route and the glossary
        def ds5():
            con = RC.Console([os.path.join(td, "runs")], log=lambda *a: None)
            code, ctype, body = con.handle("GET", "/api/dossier", {"run": [run]}, {})
            ok(code == 200 and ctype == "json" and set(body) >= {"spec", "evidence", "engine", "calibration"},
               f"DS5a /api/dossier returns the four sections ({code}, {sorted(body) if isinstance(body, dict) else body})")
            try:
                con.handle("GET", "/api/dossier", {"run": [os.path.join(td, "outside")]}, {})
                r = "served"
            except ValueError as e:
                r = str(e)
            ok(r != "served", f"DS5b an unknown run is refused ({r})")
            code, ctype, js = con.handle("GET", "/dossier.js", {}, {})
            page = con.handle("GET", "/", {}, {})[2].decode()
            ok(code == 200 and ctype.startswith("text/javascript") and '<script src="/dossier.js"></script>' in page,
               "DS5c the page loads /dossier.js from the console server (script-src 'self')")
            src = open(JS, encoding="utf-8").read()
            used = set(re.findall(r'\bT\("([^"]+)"', src))
            missing = sorted(k for k in used if k not in RC.GLOSSARY)
            ok(used and not missing, f"DS5d every glossary term the dossier marks has a definition and a source "
                                     f"({len(used)} used; missing {missing})")
        case("DS5", ds5)
        _self_test_review1(ok, td)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(2)
