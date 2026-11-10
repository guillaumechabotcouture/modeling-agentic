#!/usr/bin/env python3
"""Absolute burden validation (F6 S2-5, critique C7): the continuation arm's burden against external estimates.

    python3 toolkit/analysis/burden.py --self-test

Spec (optional section; checked by toolkit/spec/schema.py, no values in code):

    validation:
      burden:
        csv: data/burden_references.csv   # the reference estimates, under the contract below
        group: region                     # the units-table column the references' groups name (default: the region)
        arm: none                         # the continuation package (default: the comparator)
        daly_parts: {dalys: dalys, deaths: deaths, yll_per_death: {<population group> | all: <number | parameter>}}
        window: [2019, 2024]              # optional (S2-5b): the years the model value is averaged over (default: the
                                          # decision window); inside the simulated horizon
        window_metrics: {cases: <metric>} # with window: per outcome, the engine window metric that gives it per person
                                          # over a named window (all ages); every reference's outcome needs one

Reference CSV contract (COLUMNS): id; group (a value of the group column, or * for every unit); age_band (blank: all
ages, or a decision_units.populations key); outcome (one the spec reports); measure (count | rate, per `per` people of
the band); year (within max_year_gap of the decision window); value, lower, upper (finite, >= 0, lower <= value <=
upper); kind (estimated | reported: reported values are shown for information only, never judged or fitted); source
(the citation); tolerance (ratio: model / value inside [ratio_low, ratio_high], blank for the manifest default and
never wider than its widest band | overlap: the model interval meets [lower, upper]); source_id (the publication or
dataset) and derived_from (its upstream source ids, ; separated, may be empty). Optional columns (S2-5b): role (blank
or checked | report_only: computed and reported beside the checked rows, never a finding) and interval (blank or
published | none: no interval is published, lower and upper are left blank, and the row is judged on its ratio band
alone; overlap needs a published interval).

Independence is declared, not inferred: every calibration input names its source ids (observation targets per row in
their `source` column; `calibration.targets.source_id`, `calibration.target_source_id` for the inversion target column,
and `source_id` on each joint unit or group target), and a reference whose source_id or derived_from meets that set is
refused. The reference file may not be a calibration input by path, inode or content. The sign-off sheet shows the
declared sources, so the owner attests independence; the artifact records them.

The model value is the arm's annual mean over the decision window (window total / window years), summed over the
group's units. With `window`, it is the arm's mean over that window instead: the harness adds it to the engine's named
windows (Horizon.windows, `WINDOW`) in the same runs, so each run returns the engine's window metrics for one more
window and no run is added; on the emulator path one more GP per mapped outcome is fitted (a new window changes the
batch's job hashes, so a batch computed without it is not reused). Its interval comes from the uncertainty draws for
all-age references; an age-banded reference is the point value, and the artifact says so. Thresholds: toolkit/analysis/thresholds.yaml `burden`.
"""
from __future__ import annotations

import csv
import math
import os
import re
import sys

import numpy as np
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
PRODUCER = "toolkit/analysis/burden.py"
KEYS = {"csv", "group", "arm", "daly_parts", "window", "window_metrics"}
COLUMNS = ("id", "group", "age_band", "outcome", "measure", "per", "year", "value", "lower", "upper", "kind", "source",
           "tolerance", "ratio_low", "ratio_high", "source_id", "derived_from")
KINDS = ("estimated", "reported")
TOLERANCES = ("ratio", "overlap")
STATUSES = ("within", "outside", "information", "unmatched")
OPTIONAL = {"role": ("checked", "report_only"), "interval": ("published", "none")}    # S2-5b: blank = the first
WINDOW = "burden_validation"        # the engine's named window for validation.burden.window


def thresholds() -> dict:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.yaml")) as f:
        return yaml.safe_load(f)["burden"]


def section(raw):
    v = raw.get("validation") if isinstance(raw, dict) else None
    return v.get("burden") if isinstance(v, dict) else None


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def config(s) -> dict:
    b = section(s.raw) or {}
    return {"csv": b.get("csv"), "group": b.get("group") or s["decision_units"].get("region"),
            "arm": b.get("arm") or s.comparator, "daly_parts": b.get("daly_parts"), "window": b.get("window"),
            "window_metrics": b.get("window_metrics")}


def model_window(s) -> dict | None:
    """{window, metrics, arm} when validation.burden declares a well-formed model window, else None (validation
    refuses a malformed one before any run)."""
    b = section(getattr(s, "raw", None))
    w, m = (b.get("window"), b.get("window_metrics")) if isinstance(b, dict) else (None, None)
    if not (isinstance(w, list) and len(w) == 2 and all(_num(x) is not None for x in w) and isinstance(m, dict) and m):
        return None
    return {"window": [float(x) for x in w], "metrics": dict(m), "arm": config(s)["arm"]}


def csv_path(s) -> str:
    return s.resolve(config(s)["csv"])


def _band(lo, hi, th) -> tuple:
    d = th["default_ratio_band"]
    return (d[0] if lo is None else lo), (d[1] if hi is None else hi)


def _band_ok(lo, hi, th) -> bool:
    w = th["widest_ratio_band"]
    return lo is not None and hi is not None and w[0] <= lo <= 1.0 <= hi <= w[1]


def calibration_inputs(s) -> list[dict]:
    """[{path, columns}]: every file whose values a calibration fits. Target tables (calibration.targets, each
    calibration.joint target with a csv) are fitted whole (columns None); the units table holds the inversion target
    column and the joint unit targets' columns (the table itself is listed even with none)."""
    cal = s.raw.get("calibration") or {}
    joint = [t for t in ((cal.get("joint") or {}).get("targets") or []) if isinstance(t, dict)]
    out = [{"path": s.resolve(c), "columns": None} for c in
           [(cal.get("targets") or {}).get("csv")] + [t.get("csv") for t in joint] if isinstance(c, str) and c]
    cols = [cal.get("target_column")]
    for t in joint:
        if t.get("kind") == "unit":
            cols += [t.get("column", cal.get("target_column")), t.get("sd_column"), t.get("n_column")]
            cols += list(t.get("ci_columns") or [])
    out.append({"path": s.resolve(s["decision_units"]["csv"]), "columns": [c for c in cols if isinstance(c, str)]})
    return out


def _identity(path):
    """(realpath, (device, inode), sha256) of a file, or None when it cannot be read."""
    import hashlib
    try:
        st = os.stat(path)
        with open(path, "rb") as f:
            return os.path.realpath(path), (st.st_dev, st.st_ino), hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _same_file(a, b) -> bool:
    return bool(a and b and (a[0] == b[0] or a[1] == b[1] or a[2] == b[2]))


def read_reference(base, rel) -> bytes:
    """The reference file's bytes, read once through one fd under the run dir with no link followed at any component:
    an absolute path, an empty, `.` or `..` component, or a link anywhere is refused (ValueError), never normalised.
    Validation, the sign-off sheet, signing, status and the analysis all read the file through this one semantics
    (Codex #165 r4)."""
    from toolkit.analysis.artifacts import _safe_io
    if "\\" in str(rel):          # Codex #165 r5: safe_io reads a backslash as a separator, the filesystem does not
        raise ValueError(f"{rel}: the reference file path contains a backslash; use / between components")
    sio = _safe_io()
    try:
        return sio.read_nofollow(base, str(rel))[0]
    except FileNotFoundError:
        raise ValueError(f"{rel}: the reference file does not exist") from None
    except (sio.LinkRefused, OSError) as e:
        raise ValueError(f"{rel}: the reference file cannot be read as a plain path inside the run directory without "
                         f"following a link ({e})") from None


def provenance(s, data=None) -> dict:
    """The reference file's identity and the calibration inputs it was checked to be independent of. `data`: the
    bytes already read (the sheet, signing and the analysis each read the file once); its hash is of those bytes."""
    import hashlib
    path = csv_path(s)
    try:
        data = read_reference(s.base, config(s)["csv"]) if data is None else data
    except ValueError:
        data = None
    st = _identity(path)
    me = (st[0], st[1], hashlib.sha256(data).hexdigest()) if st and data is not None else None
    ins = [(c["path"], _identity(c["path"])) for c in calibration_inputs(s)]
    rel = lambda q: os.path.relpath(q, s.base) if os.path.isabs(q) else q  # noqa: E731
    refs = load_references(s, data)[0] if data is not None else []
    cids, cbad = calibration_source_ids(s)
    shared = [x["id"] for x in refs if x["source_id"] in cids or set(x["derived_from"]) & set(cids)]
    return {"csv": config(s)["csv"], "realpath": me[0] if me else None, "sha256": me[2] if me else None,
            "calibration_inputs": [{"path": rel(q), "sha256": i[2] if i else None} for q, i in ins],
            "calibration_source_ids": cids,
            "references": [{"id": x["id"], "source_id": x["source_id"], "derived_from": x["derived_from"]} for x in refs],
            "basis": "declared source ids, attested by the owner on the sign-off sheet",
            "independent": bool(me) and not any(_same_file(me, i) for _q, i in ins) and not shared and not cbad}


def sheet_lines(raw, run_dir, data=None) -> list[str]:
    """The sign-off sheet's section: each reference's declared sources beside the calibration's, which signing
    attests are independent (it cannot be inferred from the numbers)."""
    if section(raw) is None:
        return []
    out = ["## Burden validation references", ""]
    try:
        from toolkit.spec import schema
        sp = schema.Spec(raw, run_dir)
        data = read_reference(run_dir, config(sp)["csv"]) if data is None else data     # one read
        pv, (refs, bad) = provenance(sp, data), load_references(sp, data)
    except Exception as e:  # noqa: BLE001 -- the sheet says so rather than omitting the section
        return out + [f"Unreadable: {type(e).__name__}: {e}", ""]
    mw = model_window(sp)
    if mw:
        out += [f"Model values are the arm's mean over {mw['window'][0]:g}-{mw['window'][1]:g}, from the engine window "
                f"metrics {', '.join(f'{o} <- {m}' for o, m in mw['metrics'].items())} (per person over the window).",
                ""]
    out += [f"Signing attests that no reference below comes from, or is derived from, a source the calibration fits "
            f"(calibration sources: {', '.join(pv['calibration_source_ids']) or 'none declared'}). Reference file "
            f"`{pv['csv']}` (sha256 `{(pv['sha256'] or 'unreadable')[:16]}`).", "",
            "| Reference | Kind | Source id | Derived from | Citation |", "|---|---|---|---|---|"]
    out += [f"| {x['id']} | {x['kind']} | {x['source_id']} | {', '.join(x['derived_from']) or 'none'} | {x['source']} |"
            for x in refs]
    return out + [f"- problem: {b}" for b in bad] + [""]


def _ids(v):
    """A declared source id (a non-empty string) or list of them, else None."""
    v = [v] if isinstance(v, str) else v
    return [x.strip() for x in v] if isinstance(v, list) and v and all(isinstance(x, str) and x.strip() for x in v) \
        else None


def calibration_source_ids(s) -> tuple[list[str], list[str]]:
    """(the source ids of every calibration input, problems). Observation targets name their source per row (the
    table's `source` column); a target table, the inversion target column and each joint unit or group target
    declare `source_id` (a publication or dataset id, or a list), which validation.burden requires."""
    cal = s.raw.get("calibration") or {}
    tg = cal.get("targets") or {}
    ids, bad = set(), []

    def need(v, where):
        got = _ids(v)
        if got is None:
            bad.append(f"{where}: the source id (or ids) of the data this target fits")
        else:
            ids.update(got)
    if tg.get("kind") == "observations":
        try:
            with open(s.resolve(tg["csv"]), newline="", encoding="utf-8") as f:
                ids.update(str(r[tg["source"]]).strip() for r in csv.DictReader(f) if str(r[tg["source"]]).strip())
        except (OSError, KeyError, csv.Error) as e:
            bad.append(f"calibration.targets: observation sources unreadable: {e}")
    elif tg.get("csv"):
        need(tg.get("source_id"), "calibration.targets.source_id")
    if cal.get("method") == "emulator_inversion" and cal.get("target_column"):
        need(cal.get("target_source_id"), "calibration.target_source_id")
    for n, t in enumerate((cal.get("joint") or {}).get("targets") or []):
        if isinstance(t, dict) and t.get("kind") in ("unit", "group"):
            need(t.get("source_id"), f"calibration.joint.targets[{n}].source_id")
    return sorted(ids), bad


def load_references(s, data=None) -> tuple[list[dict], list[str]]:
    """(references, problems) for the spec's validation.burden: its keys and the reference CSV contract. `data`: the
    reference file's bytes when the caller already read them (else `s.burden_bytes`, else one read_reference)."""
    v = s.raw.get("validation")
    if v is None:
        return [], []
    if not isinstance(v, dict) or set(v) - {"burden"} or not isinstance(v.get("burden"), dict):
        return [], ["validation: a mapping with `burden` (the only check this section declares)"]
    b, th, P = v["burden"], thresholds(), "validation.burden"
    bad = [f"{P}: unknown key {k}" for k in sorted(set(b) - KEYS)]
    cfg = config(s)
    units = s.units()
    pops = s["decision_units"]["populations"]
    if not cfg["group"] or not units or cfg["group"] not in units[0]:
        bad.append(f"{P}.group: {cfg['group']!r} is not a units-table column")
    if cfg["arm"] not in s.packages():
        bad.append(f"{P}.arm: {cfg['arm']!r} is not a package")
    dp = cfg["daly_parts"]
    if dp is not None:
        y = dp.get("yll_per_death") if isinstance(dp, dict) else None
        names = {p.name for p in s.engine().parameters()}
        if not isinstance(dp, dict) or set(dp) - {"dalys", "deaths", "yll_per_death"} or (
                y is not None and not (isinstance(y, dict) and (set(y) == {"all"} or set(y) == set(pops))
                                       and all((_num(x) or 0) > 0 or x in names for x in y.values()))):
            bad.append(f"{P}.daly_parts: {{dalys, deaths, yll_per_death: {{all or every population group: a positive "
                       "number or an engine parameter}}}}")
    if not isinstance(cfg["csv"], str) or not cfg["csv"]:
        return [], bad + [f"{P}.csv: the reference estimates file"]
    if "\\" in cfg["csv"]:          # refused before any read (Codex #165 r5)
        return [], bad + [f"{P}.csv: {cfg['csv']!r} contains a backslash; use / between components"]
    path = s.resolve(cfg["csv"])
    me = _identity(path)
    for c in calibration_inputs(s):
        if _same_file(me, _identity(c["path"])):
            bad.append(f"{P}.csv: the same file as the calibration input {os.path.basename(c['path'])} (by path, "
                       "inode or content); references validate, they are never fitted")
    try:
        data = data if data is not None else getattr(s, "burden_bytes", None)
        data = read_reference(s.base, cfg["csv"]) if data is None else data
        import io
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8"), newline="")))
    except (ValueError, UnicodeDecodeError, csv.Error) as e:
        return [], bad + [f"{P}.csv: {e}"]
    if not rows or set(COLUMNS) - set(rows[0]):
        return [], bad + [f"{P}.csv: needs a row and the columns {', '.join(COLUMNS)}"]
    groups = {str(u.get(cfg["group"])) for u in units} if cfg["group"] and cfg["group"] in units[0] else set()
    outs = s.outcomes()
    t0, t1 = (float(x) for x in s["objective"]["horizon"])
    mw, wm = None, cfg["window_metrics"]
    if cfg["window"] is not None or wm is not None:       # S2-5b: the model window and its window metrics
        h0, h1 = float(s["horizon"]["start"]), float(s["horizon"]["stop"])
        w = [_num(x) for x in (cfg["window"] if isinstance(cfg["window"], list) and len(cfg["window"]) == 2
                               else [None])]
        if None in w or not h0 <= w[0] < w[1] <= h1:
            bad.append(f"{P}.window: [start, end], start < end, inside the simulated horizon {h0:g}-{h1:g}")
        else:
            mw, (t0, t1) = w, w
        wn = s.engine().describe().outputs.get("windows", [])
        if not (isinstance(wm, dict) and wm and all(o in outs and m in wn for o, m in wm.items())):
            bad.append(f"{P}.window_metrics: with window, {{reported outcome: an engine window metric {wn} giving "
                       "it per person over the window}")
            wm = {}
    refs, seen = [], set()
    for n, r in enumerate(rows, 2):
        e = []
        rid = str(r.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", rid) or rid in seen:
            e.append("id: unique, letters, digits, _ or -")
        seen.add(rid)
        g, band = str(r["group"]), str(r["age_band"] or "").strip()
        if g != "*" and g not in groups:
            e.append(f"group {g!r} is not a value of {cfg['group']} (or *): no unit to compare")
        if band and band not in pops:
            e.append(f"age_band {band!r} is not a decision_units.populations group")
        if mw and band:
            e.append("age_band: a model window compares all ages (window metrics are per person, all ages)")
        if mw and wm and r["outcome"] not in wm:
            e.append(f"outcome {r['outcome']!r} has no window_metrics entry for the model window")
        if r["outcome"] not in outs:
            e.append(f"outcome {r['outcome']!r} is not reported by the spec {outs}")
        per = _num(r["per"])
        if r["measure"] not in ("count", "rate") or (r["measure"] == "rate" and not (per or 0) > 0):
            e.append("measure: count, or rate with per > 0")
        yr = _num(r["year"])
        gap = None if yr is None else max(t0 - yr, yr - t1, 0.0)
        if gap is None or gap > float(th["max_year_gap"]):
            e.append(f"year {r['year']!r}: more than {th['max_year_gap']} years from the "
                     f"{'model' if mw else 'decision'} window {t0:g}-{t1:g}")
        opt = {k: str(r.get(k) or "").strip() or v[0] for k, v in OPTIONAL.items()}
        e += [f"{k} {opt[k]!r}: blank, or one of {', '.join(v)}" for k, v in OPTIONAL.items() if opt[k] not in v]
        val, lo, hi = _num(r["value"]), _num(r["lower"]), _num(r["upper"])
        if opt["interval"] == "none":           # S2-5b: no published interval; lower and upper stay blank
            if str(r["lower"] or "").strip() or str(r["upper"] or "").strip() or val is None or val < 0:
                e.append("interval none: lower and upper blank (no published interval), value finite and >= 0")
            if r["tolerance"] == "overlap":
                e.append("tolerance overlap needs a published interval; use ratio")
        elif None in (val, lo, hi) or not 0 <= lo <= val <= hi:
            e.append("value, lower, upper: finite, >= 0, lower <= value <= upper (or interval none, bounds blank)")
        if r["kind"] not in KINDS:
            e.append(f"kind {r['kind']!r}: estimated or reported")
        if not str(r["source"] or "").strip():
            e.append("source: the citation")
        sid = str(r["source_id"] or "").strip()
        der = [x.strip() for x in str(r["derived_from"] or "").split(";") if x.strip()]
        if not sid:
            e.append("source_id: the publication or dataset id (derived_from: its upstream source ids, ; separated)")
        cells = {k: str(r[k] if r[k] is not None else "").strip() for k in ("ratio_low", "ratio_high")}
        unread = [k for k, x in cells.items() if x and _num(x) is None]
        if unread:
            e.append(f"{', '.join(unread)}: blank (the manifest default) or a finite number")
        rlo, rhi = (None if not cells[k] else _num(cells[k]) for k in ("ratio_low", "ratio_high"))
        band_r = _band(rlo, rhi, th)
        if r["tolerance"] not in TOLERANCES:
            e.append(f"tolerance {r['tolerance']!r}: ratio or overlap")
        elif r["tolerance"] == "ratio" and (not _band_ok(*band_r, th) or not (val or 0) > 0):
            e.append(f"ratio band {list(band_r)} must contain 1 within {th['widest_ratio_band']}, with value > 0")
        if e:
            bad += [f"{P}.csv line {n}: {x}" for x in e]
            continue
        refs.append({"id": rid, "group": g, "age_band": band or None, "outcome": r["outcome"],
                     "measure": r["measure"], "per": per if r["measure"] == "rate" else None, "year": yr,
                     "year_gap": gap, "value": val, "lower": lo, "upper": hi, "kind": r["kind"], **opt, "window": mw,
                     "source": r["source"].strip(), "source_id": sid, "derived_from": der, "tolerance": {"type": r["tolerance"], "ratio_band": list(band_r)
                                                                  if r["tolerance"] == "ratio" else None}})
    if refs and not any(x["kind"] == "estimated" and x["role"] == "checked" for x in refs):
        bad.append(f"{P}.csv: no estimated reference among the checked rows (reported values and report-only rows "
                   "are never judged)")
    cids, cbad = calibration_source_ids(s)       # Codex r2: declared provenance, never numeric coincidence
    bad += [f"{P}: every calibration input declares its sources: {x}" for x in cbad]
    for x in refs:
        if x["source_id"] in cids:
            bad.append(f"{P}.csv reference {x['id']}: source_id {x['source_id']} is a calibration source; references "
                       "validate, they are never fitted")
        hit = sorted(set(x["derived_from"]) & set(cids))
        if hit:
            bad.append(f"{P}.csv reference {x['id']}: derived_from {hit} includes a calibration source")
    return refs, bad


# --------------------------------------------------------------------------- model values
def _years(A) -> float:
    t0, t1 = (float(x) for x in A.spec["objective"]["horizon"])
    return t1 - t0


def _members(A, cfg, g) -> list:
    return [i for i in A.ids if g == "*" or str(A.by_id[i].get(cfg["group"])) == g]


def _pop(A, ids, band) -> float:
    pops = A.spec["decision_units"]["populations"]
    cols = [pops[band]["column"]] if band else [pm["column"] for pm in pops.values()]
    return sum(float(A.by_id[i][c]) for i in ids for c in cols)


def _value(A, cfg, ref, out, arm):
    """The arm's annual mean over the window for the reference's group (a rate per `per` people of its band), or None
    when no unit (or no population, or no model-window output) matches. With a model window (S2-5b) the window is
    the reference's, and the totals are the model's `burden` outputs (window metric x population)."""
    ids = _members(A, cfg, ref["group"])
    if not ids:
        return None
    if ref.get("window"):
        try:
            tot = sum(float(out[i]["burden"][arm][ref["outcome"]]) for i in ids) / (ref["window"][1] - ref["window"][0])
        except (KeyError, TypeError, ValueError):
            return None
    else:
        tot = sum(out[i]["y"][arm][ref["outcome"]] for i in ids) / _years(A)
    if ref["measure"] == "rate":
        p = _pop(A, ids, ref["age_band"])
        return tot * ref["per"] / p if p > 0 else None
    return tot


def guard_point(A, arm, events, where="burden validation arm") -> None:
    """Model corrections (extrapolation, clips, floors) on the validation arm stop the analysis, as they do on the
    plan and the comparator (core.Analysis.guard): the arm is a plan with every unit on it."""
    A.guard(where, [{i: arm for i in A.ids}], list(events or []))


def by_population(A, theta, zs, bands, arm) -> dict:
    """{population group: outcomes of that group alone} at the given values: the outcome model predicts one population
    group at a time (its other groups and packages left out), then is restored."""
    m, res, ev = A.model, {}, []
    saved = (m.pops, m.packages, list(getattr(m, "last_events", []) or []))
    try:
        for b in bands:
            m.pops = {b: A.spec["decision_units"]["populations"][b]}
            m.packages = list(dict.fromkeys([A.comp, arm]))
            res[b] = m.predict(zs, theta, A.ids)
            ev += list(getattr(m, "last_events", []) or [])        # read before the state is restored
    finally:
        m.pops, m.packages, m.last_events = saved
    guard_point(A, arm, ev, "burden validation arm (age-band predictions)")
    return res


class Collector:
    """Each uncertainty draw's all-age reference values (UQ.run's on_draw hook)."""

    def __init__(self, A):
        self.A, self.cfg = A, config(A.spec)
        self.data = read_reference(A.spec.base, self.cfg["csv"])        # read once: values, provenance
        self.refs, bad = load_references(A.spec, self.data)
        if bad:
            raise ValueError("validation.burden: " + "; ".join(bad))
        self.draws = {r["id"]: [] for r in self.refs if not r["age_band"]}

    def add(self, out, events=(), cost=None):
        n = getattr(self, "n", 0)
        self.n = n + 1
        guard_point(self.A, self.cfg["arm"], events, f"burden validation arm, uncertainty draw {n}")
        for r in self.refs:
            if r["id"] in self.draws:
                self.draws[r["id"]].append(_value(self.A, self.cfg, r, out, self.cfg["arm"]))


def verdict(row, th=None) -> str:
    """The status a row's numbers support (the contract re-judges every row with this)."""
    th = th or thresholds()
    m, tol = row.get("model") or {}, row.get("tolerance") or {}
    p, lo, hi = _num(m.get("point")), _num(m.get("low")), _num(m.get("high"))
    if p is None or lo is None or hi is None:
        return "unmatched"
    if row.get("kind") not in KINDS:
        return "invalid"
    if row.get("kind") == "reported":
        return "information"
    if tol.get("type") == "ratio":
        b = tol.get("ratio_band") or [None, None]
        v = _num(row.get("value"))
        if not (v and v > 0 and _band_ok(_num(b[0]), _num(b[1]), th)):
            return "invalid"
        return "within" if b[0] <= p / v <= b[1] else "outside"
    if tol.get("type") == "overlap":
        rl, rh = _num(row.get("lower")), _num(row.get("upper"))
        if rl is None or rh is None or row.get("interval", "published") != "published":
            return "invalid"           # S2-5b: overlap needs a published interval
        return "within" if lo <= rh and hi >= rl else "outside"
    return "invalid"


def compare(ref, point, draws, level=0.95) -> dict:
    """One reference against the model: ratios, the model interval (draws, else the point value) and the status."""
    ok = point is not None and (draws is None or all(x is not None for x in draws))
    a = 100 * (1 - level) / 2
    if ok and draws:
        lo, med, hi = (float(np.percentile(draws, q)) for q in (a, 50, 100 - a))
        src = f"{len(draws)} uncertainty draws, central {level:.0%}"
    else:
        lo = med = hi = point
        src = ("the point value: the draws keep all-age totals only" if ref["age_band"] else
               "the point value: no uncertainty draws") if ok else "no model value: no unit or population matches"
    row = dict(ref, model={"point": point, "median": med if draws else None, "low": lo, "high": hi,
                           "interval_from": "draws" if ok and draws else "point", "note": src})
    v = ref["value"]
    row["ratio"] = point / v if ok and v > 0 else None
    row["ratio_interval"] = [point / ref["upper"] if ok and (ref["upper"] or 0) > 0 else None,
                             point / ref["lower"] if ok and (ref["lower"] or 0) > 0 else None]
    row["status"] = verdict(row)
    return row


def _param(A, x) -> float:
    if _num(x) is not None:
        return float(x)
    if x in A.theta0:
        return float(A.theta0[x])
    return float(next(p.value for p in A.eng.parameters() if p.name == x))


def daly_parts(A, cfg, arm, out0, by_pop, point) -> dict:
    """The share of the arm's DALYs from deaths (deaths x years of life lost per death), and the plan's DALYs averted
    per death averted."""
    dp = cfg["daly_parts"] or {}
    dn, de = dp.get("dalys", "dalys"), dp.get("deaths", "deaths")
    res = {"dalys": dn, "deaths": de, "deaths_share_of_dalys": None, "dalys_averted_per_death_averted": None}
    if dn not in A.outcomes or de not in A.outcomes:
        res["statement"] = f"not computed: the spec does not report both {dn} and {de}"
        return res
    av = point["averted"]
    if av[de] > 0:
        res["dalys_averted_per_death_averted"] = av[dn] / av[de]
    y = dp.get("yll_per_death")
    tot = sum(out0[i]["y"][arm][dn] for i in A.ids)
    if not y or tot <= 0:
        res["statement"] = "share from deaths not computed: " + ("no yll_per_death declared" if not y else "no DALYs")
        return res
    if "all" in y:
        yll = sum(out0[i]["y"][arm][de] for i in A.ids) * _param(A, y["all"])
    else:
        yll = sum(by_pop[b][i]["y"][arm][de] * _param(A, x) for b, x in y.items() for i in A.ids)
    res["deaths_share_of_dalys"] = yll / tot
    res["statement"] = "deaths x declared years of life lost per death, over the arm's DALYs in the window"
    return res


def run(A, out0, z0, point, col: Collector, level=0.95, events0=()) -> dict:
    """The burden_validation artifact's payload. `events0`: the point evaluation's model corrections."""
    cfg, arm, refs = col.cfg, col.cfg["arm"], col.refs
    guard_point(A, arm, events0)
    yp = (cfg["daly_parts"] or {}).get("yll_per_death") or {}
    bands = sorted({r["age_band"] for r in refs if r["age_band"]} | ({b for b in yp if b != "all"}))
    by_pop = by_population(A, A.theta0, z0, bands, arm) if bands else {}
    rows = []
    for r in refs:
        out = by_pop[r["age_band"]] if r["age_band"] else out0
        rows.append(compare(r, _value(A, cfg, r, out, arm), col.draws.get(r["id"]) or None, level))
    mw = model_window(A.spec)
    t0, t1 = mw["window"] if mw else A.spec["objective"]["horizon"]
    cnt = {s: sum(x["status"] == s for x in rows) for s in STATUSES}
    cnt["report_only"] = sum(x["role"] == "report_only" for x in rows)
    return {"arm": arm, "group_column": cfg["group"], "window": [t0, t1], "csv": cfg["csv"],
            "window_from": "validation.burden.window" if mw else "the decision window",
            "window_metrics": mw["metrics"] if mw else None,
            "provenance": provenance(A.spec, col.data),
            "draws": max((len(v) for v in col.draws.values()), default=0), "references": rows,
            "summary": dict(cnt, n=len(rows)), "daly_parts": daly_parts(A, cfg, arm, out0, by_pop, point),
            "statement": f"the {arm} arm's annual mean over {t0:g}-{t1:g} against each reference's year"}


# --------------------------------------------------------------------------- gate, claims, results.md
def _f(kind, claim, key, declarable):
    return {"kind": kind, "severity": "HIGH", "stage": "ANALYZE", "target_stage": "ANALYZE", "claim": claim,
            "key": key, "scope_declarable": declarable, "source": PRODUCER}


META = ("group", "age_band", "outcome", "measure", "per", "year", "year_gap", "value", "lower", "upper", "kind",
        "source", "source_id", "derived_from", "tolerance", "role", "interval", "window")
META_DEFAULT = {"role": "checked", "interval": "published", "window": None}     # S2-5b: artifacts written before it


def findings(raw, data, expected=None) -> list[dict]:
    """Gate findings. `expected`: the declared references (load_references of the signed spec). The artifact must hold
    each of them exactly once with its declared metadata, and at least one estimated row. A group outside its
    tolerance is declarable (the gap is declared with its decision test); a missing artifact or reference, an
    unrelated or repeated row, changed metadata, an unmatched reference or a status its numbers do not support is not
    (fail closed)."""
    if section(raw) is None:
        return []
    F = "analysis/burden_validation.yaml"
    rows = (data or {}).get("references") if isinstance(data, dict) else None
    if expected is None or not isinstance(rows, list) or not rows:
        return [_f("burden_validation_missing", f"the spec declares validation.burden but {F} has no references"
                   if expected is not None else "the declared references could not be read to check " + F,
                   "burden_validation", False)]
    exp = {r["id"]: r for r in expected}
    ids = [str(r.get("id")) if isinstance(r, dict) else None for r in rows]
    out, th = [], thresholds()
    out += [_f("burden_validation_missing", f"{F}: declared reference {i} is not in the artifact", f"burden:{i}", False)
            for i in sorted(set(exp) - set(ids))]
    extra = sorted({str(i) for i in ids if i not in exp or ids.count(i) > 1})
    out += [_f("burden_validation_unexpected", f"{F}: row {i} is not a declared reference, or appears twice",
               f"burden:{i}", False) for i in extra]
    if not any(isinstance(r, dict) and r.get("kind") == "estimated" and r.get("role", "checked") == "checked"
               for r in rows):
        out.append(_f("burden_validation_no_estimate", f"{F}: no checked estimated reference (reported values and "
                      "report-only rows are never judged, so nothing was validated)", "burden_validation", False))
    for r in rows:
        rid = str((r or {}).get("id")) if isinstance(r, dict) else None
        if rid is None or rid in extra or rid not in exp:
            continue
        diff = [k for k in META if r.get(k, META_DEFAULT.get(k)) != exp[rid].get(k, META_DEFAULT.get(k))]
        if diff:
            out.append(_f("burden_validation_inconsistent", f"{F}: reference {rid} differs from the declared file in "
                          f"{diff}", f"burden:{rid}", False))
            continue
        want = verdict(r, th)
        if r.get("status") != want or want == "invalid":
            out.append(_f("burden_validation_inconsistent", f"{F}: reference {rid} has status {r.get('status')!r}; "
                          f"its numbers give {want!r}", f"burden:{rid}", False))
        elif want == "unmatched":
            out.append(_f("burden_reference_unmatched", f"{F}: reference {rid} ({r.get('group')}) has no model value",
                          f"burden:{rid}", False))
        elif want == "outside" and r.get("role", "checked") == "checked":     # S2-5b: report-only rows never are
            m = r["model"]
            iv = (f"{r['lower']:.4g}-{r['upper']:.4g}" if r.get("interval", "published") == "published"
                  else "no published interval")
            out.append(_f("burden_outside_tolerance", (
                f"{r['outcome']} in {r['group']}{' ages ' + r['age_band'] if r.get('age_band') else ''}: model "
                f"{m['point']:.4g} ({m['low']:.4g}-{m['high']:.4g}) against the {r['kind']} {r['value']:.4g} "
                f"({iv}, {r['source']}); outside its {r['tolerance']['type']} tolerance"), f"burden:{rid}", True))
    return out


def claims(c, art, data) -> list[dict]:
    """Claim specs (toolkit/analysis/claims.py `_c`) for each reference row and the DALY parts."""
    cl = []
    for j, r in enumerate(data["references"]):
        f, k = f"data.references[{j}]", f"burden.{r['id']}"
        cl += [c(f"{k}.model", "scalar", art, f"{f}.model.point", r["outcome"]),
               c(f"{k}.model_low", "scalar", art, f"{f}.model.low", r["outcome"]),
               c(f"{k}.model_high", "scalar", art, f"{f}.model.high", r["outcome"]),
               c(f"{k}.value", "scalar", art, f"{f}.value", r["outcome"]),
               c(f"{k}.status", "label", art, f"{f}.status"), c(f"{k}.role", "label", art, f"{f}.role")]
        if r.get("interval", "published") == "published":
            cl += [c(f"{k}.lower", "scalar", art, f"{f}.lower", r["outcome"]),
                   c(f"{k}.upper", "scalar", art, f"{f}.upper", r["outcome"])]
        if r.get("ratio") is not None:
            cl.append(c(f"{k}.ratio", "scalar", art, f"{f}.ratio", "ratio"))
    dp = data["daly_parts"]
    for n in ("deaths_share_of_dalys", "dalys_averted_per_death_averted"):
        if dp.get(n) is not None:
            cl.append(c(f"burden.{n}", "scalar", art, f"data.daly_parts.{n}", "ratio"))
    return cl


def results_lines(data) -> list[str]:
    t0, t1 = data["window"]
    L = ["## Absolute burden against external estimates", "",
         f"The {data['arm']} arm's annual mean over {t0:g}-{t1:g}, by {data['group_column']}, against each reference "
         f"for its year (`validation.burden`; the window is {data.get('window_from', 'the decision window')}). "
         f"Within: {data['summary']['within']}; outside: {data['summary']['outside']}; reported values shown for "
         f"information only: {data['summary']['information']}. Rows marked report only are computed and shown "
         f"beside the checked rows and are never a finding: "
         f"{', '.join(r['id'] for r in data['references'] if r.get('role') == 'report_only') or 'none'}.",
         "", "| Reference | Group | Ages | Outcome | Year | Model | Model interval | Reference (interval) | Ratio | Kind "
         "| Tolerance | Status |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in data["references"]:
        k, tol = f"burden.{r['id']}", r["tolerance"]
        unit = f" per {r['per']:g}" if r["measure"] == "rate" else ""
        L.append(f"| {r['id']} ({r['source']}; {r['source_id']}) | {r['group']} | {r['age_band'] or 'all'} | {r['outcome']}{unit} | "
                 f"{r['year']:g} | [CLAIM:{k}.model] | [CLAIM:{k}.model_low] to [CLAIM:{k}.model_high] "
                 f"({r['model']['interval_from']}) | [CLAIM:{k}.value] "
                 + (f"([CLAIM:{k}.lower] to [CLAIM:{k}.upper])" if r.get("interval", "published") == "published"
                    else "(no published interval)") + " | "
                 + (f"[CLAIM:{k}.ratio]" if r.get("ratio") is not None else "n/a") + f" | {r['kind']} | "
                 + (f"ratio {tol['ratio_band'][0]:g} to {tol['ratio_band'][1]:g}" if tol["type"] == "ratio"
                    else "intervals overlap") + f" | [CLAIM:{k}.status]"
                 + (" (report only)" if r.get("role") == "report_only" else "") + " |")
    pts = sorted({r["model"]["note"] for r in data["references"] if r["model"]["interval_from"] == "point"})
    L += [""] + [f"Model interval from {p}." for p in pts]
    if data.get("window_metrics"):
        L.append("Model values over the window are the engine's window metrics (per person over the window, times "
                 "the population): " + ", ".join(f"{o} from {m}" for o, m in data["window_metrics"].items()) + ".")
    dp = data["daly_parts"]
    s = []
    if dp.get("deaths_share_of_dalys") is not None:
        s.append("share of the arm's DALYs from deaths [CLAIM:burden.deaths_share_of_dalys]")
    if dp.get("dalys_averted_per_death_averted") is not None:
        s.append("the plan's DALYs averted per death averted [CLAIM:burden.dalys_averted_per_death_averted]")
    s = "; ".join(s)
    L += ["", (s[:1].upper() + s[1:] + ". " if s else "") + f"({dp.get('statement', '')}.)", ""]
    return L


# --------------------------------------------------------------------------- self-test
def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    th = thresholds()
    ref = {"id": "r", "group": "g", "age_band": None, "outcome": "cases", "measure": "count", "per": None,
           "year": 1.0, "year_gap": 0.0, "value": 100.0, "lower": 80.0, "upper": 130.0, "kind": "estimated",
           "source": "synthetic", "tolerance": {"type": "ratio", "ratio_band": [0.5, 2.0]}}
    ok(compare(ref, 150.0, [140.0, 150.0, 160.0])["status"] == "within"
       and abs(compare(ref, 150.0, None)["ratio"] - 1.5) < 1e-12, "BV1 a ratio inside its band is within")
    r2 = compare(ref, 250.0, [240.0, 260.0])
    ok(r2["status"] == "outside" and r2["ratio_interval"] == [250 / 130, 250 / 80], "BV2 a ratio outside its band")
    ov = dict(ref, tolerance={"type": "overlap", "ratio_band": None})
    ok(compare(ov, 70.0, [60.0, 70.0, 85.0])["status"] == "within" and compare(ov, 70.0, [60.0, 75.0])["status"]
       == "outside", "BV3 interval overlap: the draws' interval meets the reference interval, or not")
    sec = {"validation": {"burden": {"csv": "x.csv"}}}
    rref = dict(ref, id="p", kind="reported")
    rp = compare(rref, 900.0, None)
    ok(rp["status"] == "information" and findings(sec, {"references": [compare(ref, 150.0, None), rp]}, [ref, rref])
       == [], "BV4 a reported value is information only: never judged, never a finding")
    pt = compare(dict(ref, age_band="u5"), 120.0, None)
    ok(pt["model"]["interval_from"] == "point" and "all-age" in pt["model"]["note"],
       "BV5 an age-banded reference is the point value, with a statement")
    fo = findings(sec, {"references": [r2]}, [ref])
    ok([(x["kind"], x["scope_declarable"], x["key"]) for x in fo] == [("burden_outside_tolerance", True, "burden:r")],
       f"BV6 a group outside tolerance is one declarable finding: {fo}")
    got = {x["kind"]: x["scope_declarable"] for x in findings(sec, {"references": [compare(ref, None, None)]}, [ref])}
    ok(got == {"burden_reference_unmatched": False}, f"BV7 an unmatched reference fails closed: {got}")
    wref = dict(ref, id="w", tolerance={"type": "ratio", "ratio_band": [0.01, 100.0]})
    got = [x["kind"] for x in findings(sec, {"references": [dict(r2, status="within"), compare(wref, 150.0, None)]},
                                       [ref, wref])]
    ok(got == ["burden_validation_inconsistent"] * 2,
       f"BV8 a status its numbers do not support, or a band wider than the manifest's, fails closed: {got}")
    ok([x["kind"] for x in findings(sec, None, [ref])] == ["burden_validation_missing"] and findings({}, None) == [],
       "BV9 a declared check with no artifact fails closed; no declaration, no finding")
    # Codex r1 P2: the artifact is reconciled with the declared references before any row is judged
    ok_row = compare(ref, 150.0, None)
    for name, rows, exp, want in (
            ("an unrelated row", [ok_row, compare(dict(ref, id="x"), 150.0, None)], [ref], "burden_validation_unexpected"),
            ("a declared reference missing", [ok_row], [ref, rref], "burden_validation_missing"),
            ("a reference twice", [ok_row, ok_row], [ref], "burden_validation_unexpected"),
            ("an unknown kind", [dict(ok_row, kind="modelled")], [ref], "burden_validation_inconsistent"),
            ("changed metadata", [dict(ok_row, value=140.0)], [ref], "burden_validation_inconsistent"),
            ("a reported-only artifact", [rp], [rref], "burden_validation_no_estimate"),
            ("a reported row with no model value", [ok_row, compare(rref, None, None)], [ref, rref],
             "burden_reference_unmatched")):
        got = {(x["kind"], x["scope_declarable"]) for x in findings(sec, {"references": rows}, exp)}
        ok((want, False) in got, f"BV18 the gate refuses {name}: {sorted(got)}")
    ok([x["kind"] for x in findings(sec, {"references": [ok_row]}, None)] == ["burden_validation_missing"],
       "BV18 the gate fails closed without the declared references")
    # Codex r1 P2: corrections on the validation arm stop the analysis, at the point, in every draw and per age band
    from toolkit.analysis.core import Analysis, AnalysisBlocked
    clip = [{"unit": "u1", "package": "a", "kind": "reduction_clip"}]

    class _G:
        comp, ids, outcomes, theta0 = "none", ["u1", "u2"], ["dalys", "deaths"], {}
        by_id = {"u1": {"g": "g1", "py": 100.0, "po": 300.0}, "u2": {"g": "g2", "py": 50.0, "po": 50.0}}
        spec = {"decision_units": {"populations": {"young": {"column": "py"}, "old": {"column": "po"}}},
                "objective": {"horizon": [0, 2]}}
        plan_corrections, guard = Analysis.plan_corrections, Analysis.guard
    _G = _G()

    class _M:
        clip = False

        def __init__(self):
            self.pops, self.packages, self.last_events = dict(_G.spec["decision_units"]["populations"]), ["none", "a"], []

        def predict(self, zs, theta, ids, noise=None):
            rate = {"young": {"deaths": 0.01, "dalys": 0.8}, "old": {"deaths": 0.002, "dalys": 0.1}}
            self.last_events = list(clip) if self.clip else []
            return {i: {"y": {k: {o: sum(rate[b][o] * _G.by_id[i][pm["column"]] for b, pm in self.pops.items())
                                  for o in ("deaths", "dalys")} for k in self.packages}} for i in ids}
    _G.model = _M()
    col = Collector.__new__(Collector)
    col.A, col.cfg, col.refs, col.draws = _G, {"arm": "a", "group": "g"}, [], {}
    try:
        col.add({}, clip)
        ok(False, "BV19 a correction on the validation arm in a draw stops the analysis")
    except AnalysisBlocked:
        ok(True, "BV19 a correction on the validation arm in a draw stops the analysis")
    try:
        guard_point(_G, "a", clip)
        ok(False, "BV19 a correction on the validation arm at the point stops the analysis")
    except AnalysisBlocked:
        ok(True, "BV19 a correction on the validation arm at the point stops the analysis")
    try:
        col.add({}, [dict(clip[0], package="b")])
        guard_point(_G, "a", [dict(clip[0], package="b")])
        ok(True, "BV19 a correction on another unchosen package does not")
    except AnalysisBlocked:
        ok(False, "BV19 a correction on another unchosen package does not")
    _G.model.clip, pops0 = True, _G.model.pops
    try:
        by_population(_G, {}, {}, ["young", "old"], "a")
        ok(False, "BV19 a correction in the age-band predictions stops the analysis")
    except AnalysisBlocked:
        ok(_G.model.pops is pops0 and _G.model.last_events == [],
           "BV19 a correction in the age-band predictions stops the analysis; the model state is restored")
    # genuine age separation: two bands with different rates and years of life lost per death
    _G.model.clip = False
    bp = by_population(_G, {}, {}, ["young", "old"], "a")
    full = _G.model.predict({}, {}, _G.ids)
    cfg = {"arm": "a", "group": "g", "daly_parts": {"yll_per_death": {"young": 60.0, "old": 20.0}}}
    yr = dict(ref, group="g1", age_band="young", outcome="deaths", measure="rate", per=1000.0)
    dpa = daly_parts(_G, cfg, "a", full, bp, {"averted": {"dalys": 8.0, "deaths": 0.2}})
    want = (1.5 * 60 + 0.7 * 20) / 155.0
    ok(abs(bp["young"]["u1"]["y"]["a"]["deaths"] - 1.0) < 1e-12 and abs(bp["old"]["u1"]["y"]["a"]["deaths"] - 0.6)
       < 1e-12 and abs(_value(_G, cfg, yr, bp["young"], "a") - 1000 * 0.01 / 2) < 1e-12
       and abs((dpa["deaths_share_of_dalys"] or 0) - want) < 1e-12
       and abs((dpa["dalys_averted_per_death_averted"] or 0) - 40) < 1e-9,
       f"BV20 per-band values and per-band years of life lost ({dpa['deaths_share_of_dalys']} vs {want})")
    # Codex r1 P3: zero and censored endpoints are drawn, not dropped
    from toolkit.analysis import figure_style as FS, figures as FG

    class _R:
        def art(self, k):
            z = dict(compare(dict(ov, lower=0.0), 0.0, None), id="z")
            return {"references": [z, compare(ref, 150.0, None)], "summary": {"within": 1}, "window": [0, 1],
                    "arm": "none"}
    fz = FG.fig_burden_validation(_R(), FS.setup("light"))
    zr = next(r for r in fz[3] if r["reference"] == "z")
    texts = [x.get_text() for a in fz[0].axes for x in a.texts]
    ok("model 0" in str(zr.get("shown_as")) and "reference lower 0" in str(zr.get("shown_as"))
       and any(t_.startswith("0") for t_ in texts), f"BV21 zero endpoints are shown at the axis floor: {zr.get('shown_as')}")
    ok(th["widest_ratio_band"][0] <= th["default_ratio_band"][0] <= 1 <= th["default_ratio_band"][1]
       <= th["widest_ratio_band"][1], "BV10 the manifest's default band lies within its widest band")
    # the spec contract on a toy spec
    from toolkit.spec import schema
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d)
        head = ",".join(COLUMNS)
        good = "a,r0,,cases,count,,1,100,80,130,estimated,synthetic,ratio,,,WMR-X,"

        def probs(lines, extra=None):
            open(os.path.join(d, "refs.csv"), "w").write("\n".join([head] + lines) + "\n")
            raw = yaml.safe_load(open(p))
            raw["validation"] = {"burden": dict({"csv": "refs.csv"}, **(extra or {}))}
            raw["calibration"]["target_source_id"] = "SURVEY-T"
            return load_references(schema.Spec(raw, d))[1]
        ok(probs([good]) == [], f"BV11 a valid reference file passes: {probs([good])}")
        cases = [("missing group", ["a,zz,,cases,count,,1,100,80,130,estimated,s,ratio,,,WMR-X,"], "is not a value"),
                 ("unknown band", ["a,r0,kids,cases,count,,1,100,80,130,estimated,s,ratio,,,WMR-X,"], "age_band"),
                 ("unreported outcome", ["a,r0,,deaths,count,,1,100,80,130,estimated,s,ratio,,,WMR-X,"], "outcome"),
                 ("interval", ["a,r0,,cases,count,,1,100,120,130,estimated,s,ratio,,,WMR-X,"], "lower <= value"),
                 ("kind", ["a,r0,,cases,count,,1,100,80,130,modelled,s,ratio,,,WMR-X,"], "kind"),
                 ("source", ["a,r0,,cases,count,,1,100,80,130,estimated,,ratio,,,WMR-X,"], "source"),
                 ("wide band", ["a,r0,,cases,count,,1,100,80,130,estimated,s,ratio,0.01,50,WMR-X,"], "ratio band"),
                 ("year", ["a,r0,,cases,count,,40,100,80,130,estimated,s,ratio,,,WMR-X,"], "years from"),
                 ("rate per", ["a,r0,,cases,rate,,1,100,80,130,estimated,s,ratio,,,WMR-X,"], "per > 0"),
                 ("only reported", ["a,r0,,cases,count,,1,100,80,130,reported,s,ratio,,,WMR-X,"], "no estimated"),
                 ("duplicate id", [good, good], "unique"),
                 ("unparsable band", ["a,r0,,cases,count,,1,100,80,130,estimated,s,ratio,oops,typo,WMR-X,"], "ratio_low"),
                 ("non-finite band", ["a,r0,,cases,count,,1,100,80,130,estimated,s,ratio,NaN,NaN,WMR-X,"], "ratio_low"),
                 ("no source id", ["a,r0,,cases,count,,1,100,80,130,estimated,s,ratio,,,"], "source_id: the publication"),
                 ("one bound unparsable", ["a,r0,,cases,count,,1,100,80,130,estimated,s,ratio,0.5,x,WMR-X,"], "ratio_high")]
        for name, lines, needle in cases:
            got = probs(lines)
            ok(any(needle in x for x in got), f"BV12 the reference contract refuses: {name} ({got})")
        ok(any("arm" in x for x in probs([good], {"arm": "zz"})) and any("unknown key" in x for x in
                                                                          probs([good], {"tolerance": 2})),
           "BV13 an unknown arm or key is refused")
        raw = yaml.safe_load(open(p))
        raw["validation"] = {"burden": {"csv": "units.csv"}}
        raw["calibration"]["targets"] = {"csv": "units.csv"}
        ok(any("never fitted" in x for x in load_references(schema.Spec(raw, d))[1]),
           "BV14 the calibration targets file cannot be the reference file")
        raw = yaml.safe_load(open(p))
        raw["validation"] = {"burden": {"csv": "refs.csv", "daly_parts": {"yll_per_death": {"all": -1}}}}
        ok(any("daly_parts" in x for x in load_references(schema.Spec(raw, d))[1])
           and any("validation.burden" in x for x in schema.validate(schema.Spec(raw, d))),
           "BV15 daly_parts needs positive years of life lost; schema.validate reports the section")
        # Codex r1 P1: a reference file is never a calibration input, by any name, nor do its rows repeat fitted values
        import shutil
        open(os.path.join(d, "refs.csv"), "w").write(head + "\n" + good + "\n")
        os.symlink(os.path.join(d, "refs.csv"), os.path.join(d, "alias.csv"))
        shutil.copy(os.path.join(d, "refs.csv"), os.path.join(d, "copy.csv"))

        def indep(cal_mut, ref_csv="refs.csv"):
            raw = yaml.safe_load(open(p))
            raw["validation"] = {"burden": {"csv": ref_csv}}
            cal_mut(raw["calibration"])
            return load_references(schema.Spec(raw, d))[1]
        jt = lambda f: lambda c: c.update(joint={"targets": [{"id": "g", "kind": "group", "csv": f}]})  # noqa: E731
        for name, mut, rc in (("a symlink of the targets file", lambda c: c.update(targets={"csv": "alias.csv"}), "refs.csv"),
                              ("a copy of the targets file", lambda c: c.update(targets={"csv": "copy.csv"}), "refs.csv"),
                              ("a joint-calibration group target", jt("refs.csv"), "refs.csv"),
                              ("an alias of a joint target", jt("alias.csv"), "refs.csv"),
                              ("the units table", lambda c: None, "units.csv")):
            got = indep(mut, rc)
            ok(any("calibration input" in x for x in got), f"BV17 the reference file is refused as {name}: {got}")
        # Codex r2: independence is declared provenance (source ids), never numeric equality
        u0 = next(u for u in schema.load(p).units() if u["region"] == "r0")

        def refs_with(sid, der="", value="100", group="r0"):
            open(os.path.join(d, "src.csv"), "w").write(head + "\n" + f"a,{group},,cases,count,,1,{value},0,1e9,"
                                                        f"estimated,s,overlap,,,{sid},{der}\n")

        def src_probs(cal_mut=lambda c: c.update(target_source_id="SURVEY-T")):
            return indep(cal_mut, "src.csv")
        refs_with("SURVEY-T", value=u0["tgt"])
        got = src_probs()
        ok(any("source_id SURVEY-T" in x for x in got),
           f"BV17 the same source as a calibration target, restated at group level, is refused by source id: {got}")
        refs_with("WMR-X", "DHS-1;SURVEY-T")
        got = src_probs()
        ok(any("derived_from" in x and "SURVEY-T" in x for x in got), f"BV17 a reference derived from it too: {got}")
        open(os.path.join(d, "panel.csv"), "w").write("unit,year,observed\n0,2019,5\n")
        refs_with("WMR-X", value="2019", group="*")
        got = src_probs(lambda c: c.update(target_source_id="SURVEY-T", targets={"csv": "panel.csv", "source_id": "PANEL-1"}))
        ok(got == [], f"BV17 a value that equals a fitted cell by coincidence (2019) is accepted: {got}")
        refs_with("WMR-X")
        got = src_probs(lambda c: None)
        ok(any("target_source_id" in x for x in got), f"BV17 a calibration target with no declared source id is refused: {got}")
        open(os.path.join(d, "jt2.csv"), "w").write("k,v\nr0,1\n")
        got = src_probs(lambda c: c.update(target_source_id="S", joint={"targets": [{"id": "g", "kind": "group",
                                                                                     "csv": "jt2.csv"}]}))
        ok(any("joint.targets[0].source_id" in x for x in got), f"BV17 a joint target needs its source id: {got}")
        refs_with("WMR-X", "JT")
        got = src_probs(lambda c: c.update(target_source_id="S", joint={"targets": [{"id": "g", "kind": "group",
                                                                                     "csv": "jt2.csv", "source_id": "JT"}]}))
        ok(any("JT" in x for x in got), f"BV17 a reference derived from a joint target's source is refused: {got}")
        open(os.path.join(d, "obs.csv"), "w").write("src,unit\nsero_2019,0\nhh_2020,1\n")
        raw = yaml.safe_load(open(p))
        raw["calibration"] = {"method": "posterior", "targets": {"kind": "observations", "csv": "obs.csv", "source": "src"}}
        got, _w = calibration_source_ids(schema.Spec(raw, d))
        ok({"sero_2019", "hh_2020"} <= set(got), f"BV17 observation targets name their sources in the table: {got}")
        refs_with("WMR-X", "DHS-1")
        raw = yaml.safe_load(open(p))
        raw["validation"] = {"burden": {"csv": "src.csv"}}
        raw["calibration"]["target_source_id"] = "SURVEY-T"
        pv = provenance(schema.Spec(raw, d))
        ok(pv.get("sha256") and pv.get("independent") is True and pv.get("calibration_source_ids") == ["SURVEY-T"]
           and pv.get("references") == [{"id": "a", "source_id": "WMR-X", "derived_from": ["DHS-1"]}]
           and any(x["path"].endswith("units.csv") for x in pv.get("calibration_inputs") or []),
           f"BV17 the artifact records the declared provenance: {pv}")
        yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
        from toolkit.spec import signoff
        sh = signoff.sheet(d)
        ok("## Burden validation references" in sh and "WMR-X" in sh and "SURVEY-T" in sh and "attest" in sh,
           "BV22 the sign-off sheet shows each reference's declared sources beside the calibration's, for the owner "
           "to attest")
        # Codex r3 P1: the sheet's machine-readable sources and the sign-off record bind the reference file's bytes
        import hashlib
        rp_ = os.path.join(d, "src.csv")
        sha = lambda: hashlib.sha256(open(rp_, "rb").read()).hexdigest()  # noqa: E731
        good_bytes = open(rp_, "rb").read()
        signoff.write_sheet(d)
        src_ = signoff.sheet_sources(open(os.path.join(d, signoff.SHEET)).read()) or {}
        ok(src_.get("burden_references_sha256") == sha(),
           f"BV23 the sheet's sources record the reference file's full sha256: {src_.get('burden_references_sha256')}")
        open(rp_, "wb").write(good_bytes.replace(b"WMR-X", b"WMR-Y"))
        try:
            signoff.sign(d, "lead", note="self-test")
            ok(False, "BV23 a reference file changed after the sheet was shown is refused at signing (stale sheet)")
        except ValueError as e:
            ok("stale" in str(e) or "reference" in str(e),
               f"BV23 a reference file changed after the sheet was shown is refused at signing (stale sheet): {e}")
        open(rp_, "wb").write(good_bytes)
        try:
            signoff.write_sheet(d)
            rec = signoff.sign(d, "lead", note="self-test")
            ok((rec.get("sha256") or {}).get("src.csv") == sha() and signoff.status(d) == "signed",
               f"BV24 the sign-off record binds the reference file's sha256: {rec.get('sha256')}")
        except ValueError as e:
            ok(False, f"BV24 the sign-off record binds the reference file's sha256: {e}")
        open(rp_, "wb").write(good_bytes.replace(b"WMR-X", b"WMR-Z"))
        st_after = signoff.status(d)
        open(rp_, "wb").write(good_bytes)
        os.remove(rp_)
        st_gone = signoff.status(d)
        open(rp_, "wb").write(good_bytes)
        ok(st_after == "changed" and st_gone == "changed" and signoff.status(d) == "signed",
           f"BV25 a reference file changed or removed after signing leaves the spec unsigned ({st_after}, {st_gone}); "
           "restored, it is signed again")
    # Codex #165 r4: one path semantics (no traversal, no link) and one read of the reference file
    with tempfile.TemporaryDirectory() as d:
        import hashlib
        from toolkit.spec import signoff
        p = schema.toy_spec(d)
        head = ",".join(COLUMNS)
        row = "a,r0,,cases,count,,1,100,0,1e9,estimated,s,overlap,,,WMR-X,\n"
        os.makedirs(os.path.join(d, "a"))
        os.makedirs(os.path.join(d, "b", "sub"))
        os.symlink(os.path.join("..", "b", "sub"), os.path.join(d, "a", "link"))
        open(os.path.join(d, "a", "refs.csv"), "w").write(head + "\n" + row)
        open(os.path.join(d, "b", "refs.csv"), "w").write(head + "\n" + row.replace("WMR-X", "WMR-B"))
        open(os.path.join(d, "b", "sub", "refs.csv"), "w").write(head + "\n" + row)

        def with_csv(c):
            raw = yaml.safe_load(open(p))
            raw["validation"] = {"burden": {"csv": c}}
            raw["calibration"]["target_source_id"] = "SURVEY-T"
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            return schema.load(p)
        for c in ("a/link/../refs.csv", "a/link/refs.csv", "./a/refs.csv"):
            sp = with_csv(c)
            got = load_references(sp)[1]
            try:
                signoff.sign(d, "lead", note="self-test")
                signed = True
            except ValueError:
                signed = False
            ok(any("reference file" in x for x in got) and not signed and signoff.burden_reference(sp.raw) == c,
               f"BV26 a reference path with a traversal component or a link ({c}) is refused by validation and at "
               f"signing, never normalised: {got[:1]}")
        # Codex #165 r5: two files, a/refs.csv and one literally named a\refs.csv; a backslash is refused before any
        # read, so no consumer can take one for the other
        open(os.path.join(d, "a\\refs.csv"), "w").write(head + "\n" + row.replace("WMR-X", "WMR-BS"))
        sp = with_csv("a\\refs.csv")
        got = load_references(sp)[1]
        try:
            read_reference(d, "a\\refs.csv")
            read_ok = True
        except ValueError as e:
            read_ok = "backslash" not in str(e)
        try:
            signoff.sign(d, "lead", note="self-test")
            signed = True
        except ValueError:
            signed = False
        ok(any("backslash" in x for x in got) and not read_ok and not signed,
           f"BV28 a backslash in the reference path is refused by validation, the reader and signing: {got[:1]}")
        # the sheet is rendered, validated and hashed from one read: a swap during generation cannot show old
        # provenance while binding new bytes
        sp = with_csv("a/refs.csv")
        rp_ = os.path.join(d, "a", "refs.csv")
        old_b = open(rp_, "rb").read()
        import toolkit.analysis.burden as BVm        # the module the sign-off sheet calls (this file may be __main__)
        orig = BVm.load_references
        calls = []

        def swapping(s_, data=None):
            out = orig(s_, data) if data is not None else orig(s_)
            if not calls:
                open(rp_, "wb").write(old_b.replace(b"WMR-X", b"WMR-NEW"))
            calls.append(1)
            return out
        BVm.load_references = swapping
        try:
            signoff.write_sheet(d)
        finally:
            BVm.load_references = orig
        txt = open(os.path.join(d, signoff.SHEET)).read()
        foot = (signoff.sheet_sources(txt) or {}).get("burden_references_sha256")
        try:
            signoff.sign(d, "lead", note="self-test")
            refused = False
        except ValueError as e:
            refused = "stale" in str(e)
        ok(calls and b"WMR-NEW" in open(rp_, "rb").read() and "WMR-X" in txt and "WMR-NEW" not in txt and foot == hashlib.sha256(old_b).hexdigest() and refused,
           f"BV27 a file swapped during sheet generation: the sheet shows and binds the bytes it read, and signing "
           f"refuses the new file (footer {str(foot)[:12]}, refused {refused})")
    _self_test_s25b(ok, th)
    # DALY parts arithmetic on a stub analysis
    class _A:
        outcomes = ["dalys", "deaths"]
        ids = ["u"]
        theta0 = {"le": 30.0}
    out = {"u": {"y": {"none": {"dalys": 600.0, "deaths": 10.0}}}}
    dp = daly_parts(_A, {"daly_parts": {"yll_per_death": {"all": "le"}}}, "none", out, {},
                    {"averted": {"dalys": 90.0, "deaths": 3.0}})
    ok(abs(dp["deaths_share_of_dalys"] - 0.5) < 1e-12 and abs(dp["dalys_averted_per_death_averted"] - 30) < 1e-12,
       f"BV16 share of DALYs from deaths and DALYs averted per death averted: {dp}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


class _StepEngine:
    """Self-test engine (S2-5b): one event per person-year before year 8, three from year 8 (the decision window):
    the arm's future differs from its past. Window metric `events`: events per person over a named window."""

    @staticmethod
    def _f(a, b):
        return max(0.0, min(b, 8.0) - a) + 3.0 * max(0.0, b - max(a, 8.0))

    def describe(self):
        from toolkit.engines.contract import EngineCard
        return EngineCard("step", "1", ["step rate"], {"a": [0, 1], "b": [0, 1]}, {"x": "unused"},
                          {"per_capita": ["cases", "dalys"], "windows": ["events"], "cost_drivers": []}, ["all"], {})

    def parameters(self):
        return []

    def run(self, params, unit, package, horizon, seed):
        from toolkit.engines.contract import Outcomes
        v = self._f(*horizon.window)
        return Outcomes({"cases": {"all": v}, "dalys": {"all": v}},
                        {n: {"events": self._f(*w)} for n, w in (horizon.windows or {}).items()}, {},
                        {"age_shares": {"all": 1.0}})


def _self_test_s25b(ok, th):
    """F6 S2-5b: a model window over past years, report-only rows, rows with no published interval."""
    import copy
    import tempfile
    import types
    from toolkit.spec import schema
    from toolkit.analysis import batch as B
    from toolkit.analysis.model import DirectModel
    with tempfile.TemporaryDirectory() as d:
        raw0 = yaml.safe_load(open(schema.toy_spec(d)))
        ok(B.windows(schema.Spec(raw0, d)) == {"cal": [1, 2]}, "BV34 without a model window the engine windows are "
           "unchanged (batch job hashes stay)")
        raw0.update(horizon={"start": 0, "stop": 10})
        raw0["objective"]["horizon"] = [8, 10]
        raw0["calibration"]["target_source_id"] = "SURVEY-T"
        head = ",".join(COLUMNS + ("role", "interval"))
        W = {"window": [0, 2], "window_metrics": {"cases": "events"}}

        def spec(lines, blk):
            open(os.path.join(d, "w.csv"), "w").write("\n".join([head] + lines) + "\n")
            r = copy.deepcopy(raw0)
            r["validation"] = {"burden": dict({"csv": "w.csv"}, **blk)}
            sp = schema.Spec(r, d)
            sp._engine = _StepEngine()          # the spec's engine, in place of the toy's
            return sp
        row = lambda yr=1, rest="estimated,s,ratio,,,WMR-X,,,", band="", lo="80,130": (  # noqa: E731
            f"a,*,{band},cases,count,,{yr},100,{lo},{rest}")
        sw = spec([row()], W)
        rw, bw = load_references(sw)
        s0 = spec([row(9)], {})
        r0, b0 = load_references(s0)
        ok(bw == [] and rw[0].get("window") == [0.0, 2.0] and B.windows(sw).get(WINDOW) == [0.0, 2.0],
           f"BV30 a model window inside the horizon validates and reaches the engine windows: {bw}")
        ok(any("years from" in x for x in load_references(spec([row()], {}))[1])
           and any("years from" in x for x in load_references(spec([row(9)], W))[1]),
           "BV30 the year gap is measured from the model window when one is declared, else the decision window")
        for name, blk, lines, needle in (
                ("a window outside the horizon", dict(W, window=[-1, 2]), [row()], "window"),
                ("a window with no metrics", {"window": [0, 2]}, [row()], "window_metrics"),
                ("an unknown window metric", dict(W, window_metrics={"cases": "zz"}), [row()], "window_metrics"),
                ("an age band under a window", W, [row(band="all")], "age_band"),
                ("an outcome with no window metric", W, [row().replace(",cases,", ",dalys,")], "window_metrics"),
                ("an unknown role", {}, [row(9, "estimated,s,ratio,,,WMR-X,,maybe,")], "role"),
                ("an unknown interval flag", {}, [row(9, "estimated,s,ratio,,,WMR-X,,,maybe")], "interval"),
                ("blank bounds without the flag", {}, [row(9, lo=",")], "lower"),
                ("bounds with the flag none", {}, [row(9, "estimated,s,ratio,,,WMR-X,,,none")], "interval"),
                ("overlap with no published interval", {}, [row(9, "estimated,s,overlap,,,WMR-X,,,none", lo=",")],
                 "overlap"),
                ("only report-only rows", {}, [row(9, "estimated,s,ratio,,,WMR-X,,report_only,")], "checked rows")):
            got = load_references(spec(lines, blk))[1]
            ok(any(needle in x for x in got), f"BV31 the contract refuses {name}: {got}")
        # the arm's past (1 per person-year) differs from its future (3): the model value is the past window's mean
        A = types.SimpleNamespace(spec=sw, ids=sw.unit_ids(), by_id={str(u[sw.uid]): u for u in sw.units()})
        out = DirectModel(sw).predict(None, {}, A.ids)
        pop = sum(float(u["pop"]) for u in sw.units())
        vw, v0 = _value(A, config(sw), rw[0], out, "none"), _value(A, config(s0), r0[0], out, "none")
        ok(vw is not None and abs(vw - pop) < 1e-9 * pop and abs(v0 - 3 * pop) < 1e-9 * pop,
           f"BV29 with a model window the value is the arm's mean over it ({vw} vs {pop}), not the decision-window "
           f"mean ({v0})")
        ok(_value(A, config(sw), rw[0], {i: {"y": out[i]["y"]} for i in A.ids}, "none") is None,
           "BV29 a windowed reference with no window output has no model value (fails closed as unmatched)")
        # report-only rows and rows with no published interval
        lines = [row(9, "estimated,s,ratio,,,WMR-X,,report_only,"), row(9, "estimated,s,ratio,,,WMR-Y,,,none", lo=",")
                 .replace("a,", "b,", 1)]
        refs, bad = load_references(spec(lines, {}))
        by = {r["id"]: r for r in refs}
        ok(bad == [] and by.get("a", {}).get("role") == "report_only" and by.get("b", {}).get("lower") is None
           and by.get("b", {}).get("interval") == "none" and by.get("a", {}).get("interval") == "published",
           f"BV32 report-only rows and rows with no published interval validate: {bad}")
        if bad:
            return
        rows = [compare(by["a"], 300.0, None), compare(by["b"], 300.0, None)]
        sec = {"validation": {"burden": {"csv": "w.csv"}}}
        fo = findings(sec, {"references": rows}, refs)
        ok(rows[0]["status"] == "outside" and [x["key"] for x in fo] == ["burden:b"]
           and "no published interval" in fo[0]["claim"] and rows[1]["ratio_interval"] == [None, None],
           f"BV32 a report-only row is judged and reported, never a finding; a row with no published interval is "
           f"judged on its tolerance band alone: {[x['claim'] for x in fo]}")
        ok([x["kind"] for x in findings(sec, {"references": [dict(rows[0], role="checked"), rows[1]]}, refs)]
           == ["burden_validation_inconsistent", "burden_outside_tolerance"],
           "BV32 an artifact that changes a row's role is inconsistent")
        data = {"window": [8, 10], "arm": "none", "group_column": "region", "references": rows,
                "summary": {"within": 0, "outside": 2, "information": 0, "unmatched": 0, "n": 2},
                "daly_parts": {"statement": "x"}}
        txt = "\n".join(results_lines(data))
        ids = {c[0] for c in claims(lambda *a: a, "art", data)}
        ok("report only" in txt and "a" in txt.split("report only")[1][:80] and "no published interval" in txt
           and "burden.a.role" in ids and "burden.b.lower" not in ids and "burden.a.lower" in ids,
           f"BV33 results.md and the claims say which rows were report-only and which have no published interval")
        ok(verdict(dict(rows[1], tolerance={"type": "overlap", "ratio_band": None}), th) == "invalid",
           "BV33 an overlap tolerance with no published interval is invalid in an artifact")


if __name__ == "__main__":
    sys.exit(_self_test() if sys.argv[1:2] == ["--self-test"] else (print(__doc__) or 2))
