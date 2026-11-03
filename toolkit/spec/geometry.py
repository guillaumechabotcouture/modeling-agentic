#!/usr/bin/env python3
"""Input geometry check: the units table's coordinates and areas against declared boundaries, before SIGN and at the
gate (design: docs/design/v2-input-geometry.md).

    python3 toolkit/spec/geometry.py check <run_dir>      # prints the counts (the harness writes the record)
    python3 toolkit/spec/geometry.py table --boundaries B.geojson --feature-id F [--crosswalk X.csv --xw-unit U
        --xw-feature G] --units T.csv --id I --lat LAT --lon LON [--area A]   # ad hoc: prints counts, writes nothing
    python3 toolkit/spec/geometry.py --self-test

Inputs. The boundaries come from the run's unsigned presentation file (presentation.yaml, or labels.yaml), the same
declaration the figures read: `boundaries: {path, feature_id, crosswalk: {path, unit_id, feature_id}, credit}`. The
units table is the spec's decision_units.csv; its point and area columns are `decision_units.coordinates: {lat, lon,
area_km2}`, or columns named lat/latitude and lon/lng/long/longitude (the figures' convention) and area_km2. Without
boundaries or without coordinates the check does not run, and says so ("not run: ...").

Checks (pure Python and json; distances on a plane scaled by cos(latitude), areas by equal-area integration):
  reciprocal_swap            HIGH  unit A's point is in B's polygon and B's point is in A's: the two rows'
                                   geometry-derived fields are probably exchanged
  point_outside_own_polygon  HIGH  the point lies farther than edge_tolerance_km outside its own polygon
                             LOW   within the tolerance (an edge case, listed, never blocking)
  area_mismatch              MEDIUM the declared area and the polygon area differ by more than area_ratio_max;
                             HIGH  when the declared area matches the polygon the point falls in better
  unmatched                  MEDIUM a unit with no polygon (or no usable point) (a polygon two units claim is refused)
A file that cannot be read or parsed is geometry_check_failed (HIGH): never a silent pass.

Exceptions. `decision_units.geometry_accepted: {<finding id>: "<reason>"}` in the spec holds proposals. A MEDIUM or
LOW one is accepted by its entry. A HIGH one counts only once the owner approves it: the owner's sign-off (or
`python3 toolkit/spec/geometry.py approve <run_dir> --by owner` after it) records the approval in
data/geometry_signoff.json, bound to the evidence hash of the current inputs. A lead sign-off approves nothing, and
the run pauses at SIGN for the owner. A HIGH with no proposal sends the spec back to the planner.

Freeze. Every sign-off writes data/geometry_signoff.json: the geometry declaration (enabled or not, the boundaries
and crosswalk as declared, the columns) and the sha256 of the boundary, crosswalk and units files. Any later change
is geometry_inputs_changed_after_signoff (HIGH), and an approval lapses when its evidence changes. Labels stay
unsigned. Areas: equal-area integration of edges straight in longitude/latitude (exact for such edges; resampling
an edge leaves the area unchanged). Thresholds: .claude/orchestration/effort_floors.yaml `input_checks.geometry`.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PRODUCER = "toolkit/spec/geometry.py"
ARTIFACT = "data/input_checks.json"
SPEC_FILE = "decision_spec.yaml"
PRESENTATION_FILES = ("presentation.yaml", "labels.yaml")
THRESHOLDS_FILE = os.path.join(ROOT, ".claude", "orchestration", "effort_floors.yaml")
CODES = ("reciprocal_swap", "point_outside_own_polygon", "area_mismatch", "unmatched")
FAILED, NOT_RUN, ACCEPT_BAD, EDGE = ("geometry_check_failed", "geometry_check_not_run", "geometry_acceptance_invalid",
                                     "point_near_own_boundary")
CHANGED = "geometry_inputs_changed_after_signoff"
MISSING = "geometry_record_missing"       # a signed spec with no geometry sign-off record (Codex round 2 on #131)
SPEC_SIGNOFFS = ("decision_spec.signoff.json", "decision_brief.signoff.json")   # the spec's own; the brief's
ACCEPT_KEY = "geometry_accepted"          # in the spec: proposals (a HIGH one counts only once the owner approves it)
# The harness's geometry sign-off record (written by the sign-off and by `approve --by owner`; no task can write it):
# the frozen declaration, the input hashes, and the owner's approvals of HIGH exceptions bound to those hashes.
SIGNOFF_FILE = "data/geometry_signoff.json"
KM_PER_DEG = math.pi / 180.0 * 6371.0088          # mean Earth radius (IUGG)
LAT_NAMES, LON_NAMES, AREA_NAMES = ("lat", "latitude"), ("lon", "lng", "long", "longitude"), ("area_km2",)


class GeometryError(ValueError):
    """An input the check cannot read: the caller reports geometry_check_failed (fail closed)."""


# --------------------------------------------------------------------------- thresholds
def thresholds(path: str | None = None) -> dict:
    """{edge_tolerance_km, area_ratio_max} from effort_floors.yaml `input_checks.geometry` (fail closed)."""
    import yaml
    p = path or THRESHOLDS_FILE
    try:
        doc = yaml.safe_load(open(p, encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise GeometryError(f"{p}: unreadable thresholds: {e}") from e
    g = ((doc.get("input_checks") or {}) if isinstance(doc, dict) else {}).get("geometry")
    out = {}
    for k in ("edge_tolerance_km", "area_ratio_max"):
        v = g.get(k) if isinstance(g, dict) else None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            raise GeometryError(f"{p}: input_checks.geometry.{k} must be a positive number")
        out[k] = float(v)
    if out["area_ratio_max"] <= 1:
        raise GeometryError(f"{p}: input_checks.geometry.area_ratio_max must exceed 1")
    return out


# --------------------------------------------------------------------------- declarations
def boundaries_config(b, base: str, shown: str) -> dict:
    """The presentation file's `boundaries` block, the one parser for the figures and this check
    (docs/design/v2-figures.md): path and feature_id (strings), an optional crosswalk {path, unit_id, feature_id,
    union} and credit; paths relative to the file. `union: true` lets a unit map to several features (drawn and
    checked as their union)."""
    def where(p):
        p = os.path.expanduser(str(p))
        return p if os.path.isabs(p) else os.path.join(base, p)
    if not isinstance(b, dict) or not isinstance(b.get("path"), str) or not isinstance(b.get("feature_id"), str):
        raise GeometryError(f"{shown}: boundaries needs path and feature_id (strings)")
    unknown = sorted(set(b) - {"path", "feature_id", "crosswalk", "credit"})
    if unknown:
        raise GeometryError(f"{shown}: boundaries: unknown keys {unknown}")
    out = {"path": where(b["path"]), "shown": b["path"], "feature_id": b["feature_id"], "credit": b.get("credit")}
    xw = b.get("crosswalk")
    if xw is not None:
        if not isinstance(xw, dict) or not all(isinstance(xw.get(k), str) for k in ("path", "unit_id", "feature_id")):
            raise GeometryError(f"{shown}: boundaries.crosswalk needs path, unit_id and feature_id (strings)")
        unknown = sorted(set(xw) - {"path", "unit_id", "feature_id", "union"})
        if unknown:
            raise GeometryError(f"{shown}: boundaries.crosswalk: unknown keys {unknown}")
        if "union" in xw and not isinstance(xw["union"], bool):
            raise GeometryError(f"{shown}: boundaries.crosswalk.union must be true or false")
        out["crosswalk"] = {"path": where(xw["path"]), "shown": xw["path"], "unit_id": xw["unit_id"],
                            "feature_id": xw["feature_id"], "union": bool(xw.get("union", False))}
    if out.get("credit") is not None and not isinstance(out["credit"], str):
        raise GeometryError(f"{shown}: boundaries.credit must be a string")
    return out


def presentation_boundaries(run_dir: str) -> tuple[dict | None, str | None]:
    """(boundaries config or None, the presentation file's name or None). Both presentation files, an unreadable
    file, or a malformed block: GeometryError."""
    import yaml
    found = [n for n in PRESENTATION_FILES if os.path.exists(os.path.join(run_dir, n))]
    if len(found) > 1:
        raise GeometryError(f"{run_dir}: both presentation.yaml and labels.yaml; keep one presentation file")
    if not found:
        return None, None
    path = os.path.join(run_dir, found[0])
    try:
        doc = yaml.safe_load(open(path, encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise GeometryError(f"{found[0]}: unreadable presentation file: {e}") from e
    doc = {} if doc is None else doc
    if not isinstance(doc, dict):
        raise GeometryError(f"{found[0]}: a mapping with labels and/or boundaries")
    if doc.get("boundaries") is None:
        return None, found[0]
    return boundaries_config(doc["boundaries"], os.path.dirname(path), found[0]), found[0]


def unit_columns(du: dict, header: list[str]) -> tuple[dict, list[str]]:
    """({id, lat, lon, area, from}, problems). Declared decision_units.coordinates {lat, lon, area_km2} win; else
    the figures' column names. A declared column the table lacks is a problem (fail closed)."""
    c = du.get("coordinates")
    bad = []
    if c is not None and not (isinstance(c, dict) and all(isinstance(c.get(k), str) and c[k].strip()
                                                           for k in c) and set(c) <= {"lat", "lon", "area_km2"}):
        return {}, ["decision_units.coordinates: a mapping {lat, lon, area_km2} of column names"]
    c = c or {}
    low = {h.lower(): h for h in header}
    lat = c.get("lat") or next((low[k] for k in LAT_NAMES if k in low), None)
    lon = c.get("lon") or next((low[k] for k in LON_NAMES if k in low), None)
    area = c.get("area_km2") or next((low[k] for k in AREA_NAMES if k in low), None)
    for k, v in (("lat", c.get("lat")), ("lon", c.get("lon")), ("area_km2", c.get("area_km2"))):
        if v and v not in header:
            bad.append(f"decision_units.coordinates.{k}: the units table has no column {v}")
    return {"id": du.get("id"), "lat": lat, "lon": lon, "area": area,
            "from": "declared" if c else "column names"}, bad


# --------------------------------------------------------------------------- reading
def _ring(r, k, shown):
    if not isinstance(r, list) or len(r) < 3:
        raise GeometryError(f"{shown}: feature {k} has a ring with fewer than 3 positions")
    out = []
    for pt in r:
        if not (isinstance(pt, (list, tuple)) and len(pt) >= 2 and all(isinstance(v, (int, float))
                                                                       and not isinstance(v, bool) for v in pt[:2])):
            raise GeometryError(f"{shown}: feature {k} has malformed coordinates")
        x, y = float(pt[0]), float(pt[1])
        if not (math.isfinite(x) and math.isfinite(y)):
            raise GeometryError(f"{shown}: feature {k} has malformed coordinates (not finite)")
        if not (-180 <= x <= 180 and -90 <= y <= 90):
            raise GeometryError(f"{shown}: feature {k} has coordinates outside the geographic range (longitude "
                                "within ±180, latitude within ±90): a projected file? Reproject to EPSG:4326")
        out.append((x, y))
    if out[0] == out[-1]:
        out = out[:-1]
    if len(set(out)) < 3:
        raise GeometryError(f"{shown}: feature {k} has a ring with fewer than 3 distinct positions")
    x0, y0 = out[0]
    if all(abs((x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)) <= 1e-12 for (x1, y1), (x2, y2) in zip(out, out[1:])):
        raise GeometryError(f"{shown}: feature {k} has a ring whose vertices are collinear")
    if _ring_area(out) <= 1e-9:                       # km2: a ring that encloses nothing
        raise GeometryError(f"{shown}: feature {k} has a ring with no area")
    return out


GEOGRAPHIC_CRS = {"EPSG:4326", "urn:ogc:def:crs:EPSG::4326", "urn:ogc:def:crs:OGC:1.3:CRS84", "OGC:CRS84", "CRS84",
                  "urn:ogc:def:crs:OGC::CRS84"}


def read_features(cfg: dict) -> dict:
    """{feature id: [polygon]} with polygon = [outer ring, hole, ...] and ring = [(lon, lat)], from a GeoJSON
    FeatureCollection of Polygon and MultiPolygon features in geographic coordinates (EPSG:4326 / CRS84). The one
    reader for the figures and this check. Fail closed (GeometryError): an unreadable file, a projected CRS,
    coordinates outside the geographic range, a malformed or degenerate ring, and a feature id used by two features.
    An empty MultiPolygon is a feature with no polygon ([]): its unit is unmatched."""
    shown = cfg.get("shown") or cfg["path"]
    try:
        with open(cfg["path"], encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        raise GeometryError(f"{shown}: unreadable boundary file: {e}") from e
    feats = doc.get("features") if isinstance(doc, dict) else None
    if not isinstance(feats, list) or not feats:
        raise GeometryError(f"{shown}: not a GeoJSON FeatureCollection with features")
    crs = doc.get("crs")
    name = ((crs.get("properties") or {}).get("name") if isinstance(crs, dict) else None)
    if crs is not None and name not in GEOGRAPHIC_CRS:
        raise GeometryError(f"{shown}: CRS {name or crs!r} is not geographic (EPSG:4326 / CRS84 only); reproject the "
                            "file")
    out: dict = {}
    dup = set()
    for k, f in enumerate(feats):
        g = f.get("geometry") if isinstance(f, dict) else None
        props = f.get("properties") if isinstance(f, dict) else None
        if not isinstance(g, dict) or not isinstance(props, dict):
            raise GeometryError(f"{shown}: feature {k} lacks geometry or properties")
        if props.get(cfg["feature_id"]) in (None, ""):
            raise GeometryError(f"{shown}: feature {k} has no property {cfg['feature_id']}")
        typ, co = g.get("type"), g.get("coordinates")
        polys = [co] if typ == "Polygon" else co if typ == "MultiPolygon" else None
        if not isinstance(polys, list) or (typ == "Polygon" and not co):
            raise GeometryError(f"{shown}: feature {k} geometry {typ!r} is not a Polygon or MultiPolygon")
        parts = []
        for poly in polys:
            if not isinstance(poly, list) or not poly:
                raise GeometryError(f"{shown}: feature {k} has malformed coordinates")
            parts.append([_ring(r, k, shown) for r in poly])
        fid = str(props[cfg["feature_id"]])
        if fid in out:
            dup.add(fid)
        out[fid] = parts
    if dup:
        raise GeometryError(f"{shown}: feature id {cfg['feature_id']} is not unique: {sorted(dup)[:20]}")
    return out


def read_crosswalk(c: dict) -> dict:
    """{unit id: [feature id, ...]} in file order; rows with an empty feature id are skipped (no polygon)."""
    shown = c.get("shown") or c["path"]
    try:
        with open(c["path"], newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, csv.Error, UnicodeDecodeError) as e:
        raise GeometryError(f"{shown}: unreadable crosswalk: {e}") from e
    if not rows or c["unit_id"] not in rows[0] or c["feature_id"] not in rows[0]:
        raise GeometryError(f"{shown}: the crosswalk needs columns {c['unit_id']} and {c['feature_id']}")
    out: dict = {}
    for r in rows:
        u, fid = str(r[c["unit_id"]] or "").strip(), str(r[c["feature_id"]] or "").strip()
        if not u or not fid:
            continue
        fl = out.setdefault(u, [])
        if fid not in fl:
            fl.append(fid)
    return out


def _orient(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _edges(polys):
    for p in polys:
        for ring in p:
            n = len(ring)
            for i in range(n):
                yield ring[i], ring[(i + 1) % n]


def _interior_point(part):
    """A point strictly inside a polygon part (outside its holes): the vertex mean, else a diagonal midpoint."""
    outer = part[0]
    cands = [(sum(x for x, _ in outer) / len(outer), sum(y for _, y in outer) / len(outer))]
    cands += [((outer[0][0] + v[0]) / 2, (outer[0][1] + v[1]) / 2) for v in outer[2:]]
    cands += [((a[0] + c[0]) / 2, (a[1] + c[1]) / 2) for a, c in zip(outer, outer[2:])]
    for x, y in cands:
        if contains([part], x, y) and distance_km([part], x, y) > 1e-6:
            return x, y
    return None


def _strictly_inside(polys, x, y) -> bool:
    return contains(polys, x, y) and distance_km(polys, x, y) > 1e-6


def overlap(a, b) -> bool:
    """Do two features' interiors overlap? Touching along edges or at vertices does not count. Proper edge
    crossings, a vertex or an interior point of one strictly inside the other do."""
    ax0, ay0, ax1, ay1 = bbox(a)
    bx0, by0, bx1, by1 = bbox(b)
    if ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0:
        return False
    eb = list(_edges(b))
    eps = 1e-12
    for p1, p2 in _edges(a):
        for q1, q2 in eb:
            o1, o2 = _orient(p1, p2, q1), _orient(p1, p2, q2)
            o3, o4 = _orient(q1, q2, p1), _orient(q1, q2, p2)
            if ((o1 > eps and o2 < -eps) or (o1 < -eps and o2 > eps)) and \
                    ((o3 > eps and o4 < -eps) or (o3 < -eps and o4 > eps)):
                return True
    for x, y in [v for p in a for v in p[0]]:
        if _strictly_inside(b, x, y):
            return True
    for x, y in [v for p in b for v in p[0]]:
        if _strictly_inside(a, x, y):
            return True
    for one, other in ((a, b), (b, a)):
        for part in one:
            q = _interior_point(part)
            if q is not None and _strictly_inside(other, *q):
                return True
    return False


def resolve_targets(xw: dict | None, unit_ids, union: bool, shown: str, feats: dict | None = None) -> dict:
    """{unit id: [feature id, ...]} for the units of the table: through the crosswalk, or the unit id itself as the
    feature id. Refused (GeometryError naming every affected unit): a unit mapped to several features without
    `crosswalk.union: true`, a feature mapped to two units, and (given `feats`) union members whose interiors
    overlap: the union's area would count the overlap twice (focused check of #131; refusing is simpler than
    dissolving the union, and a boundary set whose features overlap is itself suspect)."""
    ids = [str(u) for u in unit_ids]
    targets = {u: list((xw or {}).get(u, [])) if xw is not None else [u] for u in ids}
    multi = sorted(u for u, fl in targets.items() if len(fl) > 1)
    if multi and not union:
        raise GeometryError(f"{shown}: {len(multi)} units map to more than one feature: {multi[:20]}; declare "
                            "crosswalk.union: true to draw each such unit as the union of its features")
    users: dict = {}
    for u, fl in targets.items():
        for fid in fl:
            users.setdefault(fid, []).append(u)
    shared = {fid: us for fid, us in users.items() if len(us) > 1}
    if shared:
        aff = sorted({u for us in shared.values() for u in us})
        raise GeometryError(f"{shown}: {len(shared)} features map to more than one unit (units {aff[:20]}); each "
                            "feature may belong to one unit")
    if feats is not None:
        bad = []
        for u, fl in targets.items():
            fs = [f for f in fl if feats.get(f)]
            if any(overlap(feats[fs[i]], feats[fs[j]]) for i in range(len(fs)) for j in range(i + 1, len(fs))):
                bad.append(u)
        if bad:
            raise GeometryError(f"{shown}: {len(bad)} units' union members overlap (units {sorted(bad)[:20]}): the "
                                "union's area would count the overlap twice; fix the boundary set so a unit's "
                                "features only touch")
    return targets


def read_table(path: str) -> list[dict]:
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except (OSError, csv.Error, UnicodeDecodeError) as e:
        raise GeometryError(f"{path}: unreadable units table: {e}") from e


# --------------------------------------------------------------------------- planar geometry
def _in_ring(ring, x, y) -> bool:
    """Ray casting (even-odd) on one ring."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def contains(polys, x, y) -> bool:
    """Inside a part's outer ring and in none of its holes, for any part."""
    return any(_in_ring(p[0], x, y) and not any(_in_ring(h, x, y) for h in p[1:]) for p in polys)


def bbox(polys) -> tuple:
    xs = [x for p in polys for x, _ in p[0]]
    ys = [y for p in polys for _, y in p[0]]
    return min(xs), min(ys), max(xs), max(ys)


def distance_km(polys, x, y) -> float:
    """Shortest distance from the point to any ring edge, on a plane scaled by cos(latitude) at the point."""
    kx, ky = KM_PER_DEG * math.cos(math.radians(y)), KM_PER_DEG
    best = math.inf
    for p in polys:
        for ring in p:
            n = len(ring)
            for i in range(n):
                ax, ay = (ring[i][0] - x) * kx, (ring[i][1] - y) * ky
                bx, by = (ring[(i + 1) % n][0] - x) * kx, (ring[(i + 1) % n][1] - y) * ky
                dx, dy = bx - ax, by - ay
                L = dx * dx + dy * dy
                t = 0.0 if L == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L))
                best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def _ring_area(ring) -> float:
    """km2 enclosed by a ring on the sphere, its edges straight in longitude/latitude: the equal-area integral
    R^2 |loop-integral of sin(lat) d(lon)|, exact for each such edge (sin(lat) integrated along the edge's linear
    path). Splitting an edge with a collinear point splits its integral, so resampling leaves the area unchanged."""
    s = 0.0
    n = len(ring)
    for i in range(n):
        l1, p1 = math.radians(ring[i][0]), math.radians(ring[i][1])
        l2, p2 = math.radians(ring[(i + 1) % n][0]), math.radians(ring[(i + 1) % n][1])
        h = (p2 - p1) / 2
        s += (l2 - l1) * math.sin((p1 + p2) / 2) * (math.sin(h) / h if h else 1.0)
    return abs(s) * (KM_PER_DEG * 180 / math.pi) ** 2


def area_km2(polys) -> float:
    """Outer rings less holes, summed over parts (equal-area integration, _ring_area)."""
    return max(sum(_ring_area(p[0]) - sum(_ring_area(h) for h in p[1:]) for p in polys), 0.0)


# --------------------------------------------------------------------------- the check
def _num(v):
    try:
        x = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _ratio(a, b):
    return math.inf if min(a, b) <= 0 else max(a, b) / min(a, b)


def evaluate(rows: list[dict], cols: dict, feats: dict, targets: dict | None, th: dict) -> dict:
    """The four checks on parsed inputs: {counts, findings, checks}. Pure: no files. `targets` is
    resolve_targets()'s {unit: [feature id, ...]} (None: each unit id is its feature id); a unit with several
    features (crosswalk.union) is checked against their union."""
    uid, latc, lonc, areac = cols["id"], cols["lat"], cols["lon"], cols.get("area")
    tol, rmax = th["edge_tolerance_km"], th["area_ratio_max"]
    ids = [str(r.get(uid, "")).strip() for r in rows]
    by = dict(zip(ids, rows))
    if targets is None:
        targets = {u: [u] for u in ids}
    live = {fid: p for fid, p in feats.items() if p}             # features with at least one polygon
    boxes = {fid: bbox(p) for fid, p in live.items()}
    farea = {fid: area_km2(p) for fid, p in live.items()}
    findings, own, poly, pt = [], {}, {}, {}
    for u in ids:
        fl = targets.get(u, [])
        why = []
        if not fl:
            why.append("no crosswalk row")
        missing = [f for f in fl if f not in feats]
        empty = [f for f in fl if f in feats and not feats[f]]
        if missing:
            why.append(f"feature(s) {', '.join(missing)} not in the boundary file")
        if empty:
            why.append(f"feature(s) {', '.join(empty)} have no polygon")
        if fl and not missing and not empty:
            own[u] = list(fl)
            poly[u] = [p for f in fl for p in feats[f]]
        lon, lat = _num(by[u].get(lonc)), _num(by[u].get(latc))
        if lon is None or lat is None or not (-180 <= lon <= 180 and -90 <= lat <= 90):
            why.append("no usable coordinates")
        else:
            pt[u] = (lon, lat)
        if why:
            findings.append({"id": f"unmatched:{u}", "code": "unmatched", "severity": "MEDIUM", "units": [u],
                             "claim": f"unit {u}: {' and '.join(why)}; its point and area cannot be checked",
                             "evidence": {"features": fl}})
    owner_of = {f: u for u, fl in own.items() for f in fl}       # one unit per feature (resolve_targets)
    uarea = {u: sum(farea[f] for f in fl) for u, fl in own.items()}

    def holders(x, y):
        return [f for f, (x0, y0, x1, y1) in boxes.items() if x0 <= x <= x1 and y0 <= y <= y1
                and contains(live[f], x, y)]
    inside, outside = {}, {}
    for u in own:
        if u not in pt:
            continue
        inside[u] = contains(poly[u], *pt[u])
        if not inside[u]:
            outside[u] = {"distance_km": distance_km(poly[u], *pt[u]),
                          "in": [f for f in holders(*pt[u]) if f not in own[u]]}
    swapped: dict = {}
    for a in sorted(outside):
        for fb in outside[a]["in"]:
            b = owner_of.get(fb)
            # a pair only when both points lie outside their own polygons (overlapping or nested polygons can
            # put b's point in a's polygon while it is also inside b's own)
            if b is not None and b != a and b in outside and a not in swapped and b not in swapped \
                    and contains(poly[a], *pt[b]):
                swapped[a], swapped[b] = b, a
    area_of = {u: _num(by[u].get(areac)) for u in own} if areac else {}
    done = set()
    for a in sorted(swapped):
        b = swapped[a]
        if a in done:
            continue
        done |= {a, b}
        ev = {"features": {a: "+".join(own[a]), b: "+".join(own[b])},
              "distance_km": {a: round(outside[a]["distance_km"], 2), b: round(outside[b]["distance_km"], 2)}}
        if areac and area_of.get(a) and area_of.get(b):
            own_r = [_ratio(area_of[a], uarea[a]), _ratio(area_of[b], uarea[b])]
            sw_r = [_ratio(area_of[a], uarea[b]), _ratio(area_of[b], uarea[a])]
            ev["area_ratio_own"], ev["area_ratio_swapped"] = [round(r, 3) for r in own_r], [round(r, 3) for r in sw_r]
            ev["areas_consistent_with_swap"] = max(sw_r) < min(own_r) or (max(sw_r) <= rmax < max(own_r))
        findings.append({"id": f"reciprocal_swap:{a}+{b}", "code": "reciprocal_swap", "severity": "HIGH",
                         "units": [a, b], "evidence": ev,
                         "claim": f"units {a} and {b}: each unit's point lies inside the other's polygon; the two "
                                  "rows' geometry-derived fields (coordinates, area, and anything computed from "
                                  "them, such as population or raster values) are probably exchanged"
                                  + ("; the declared areas match the swapped polygons better"
                                     if ev.get("areas_consistent_with_swap") else "")})
    for u in sorted(outside):
        if u in swapped:
            continue
        o = outside[u]
        other = o["in"]
        ev = {"feature": "+".join(own[u]), "distance_km": round(o["distance_km"], 2), "in_features": other,
              "in_units": sorted({owner_of[f] for f in other if f in owner_of})}
        if o["distance_km"] <= tol:
            findings.append({"id": f"point_outside_own_polygon:{u}", "code": "point_outside_own_polygon",
                             "severity": "LOW", "edge": True, "units": [u], "evidence": ev,
                             "claim": f"unit {u}: the point is {o['distance_km']:.1f} km outside its own polygon "
                                      f"(within the {tol:g} km edge tolerance)"})
        else:
            where = (f"; it lies in feature {other[0]}" + (f" (unit {', '.join(ev['in_units'])})" if ev["in_units"]
                                                           else "") if other else "; it lies in no polygon")
            findings.append({"id": f"point_outside_own_polygon:{u}", "code": "point_outside_own_polygon",
                             "severity": "HIGH", "units": [u], "evidence": ev,
                             "claim": f"unit {u}: the point is {o['distance_km']:.1f} km outside its own polygon "
                                      f"(edge tolerance {tol:g} km){where}"})
    if areac:
        for u in sorted(own):
            if u in swapped:
                continue
            d, pa = area_of.get(u), uarea[u]
            if d is None or d <= 0:
                findings.append({"id": f"area_mismatch:{u}", "code": "area_mismatch", "severity": "MEDIUM",
                                 "units": [u], "evidence": {"declared_km2": d, "polygon_km2": round(pa, 1)},
                                 "claim": f"unit {u}: no positive declared area in column {areac}"})
                continue
            r = _ratio(d, pa)
            if r <= rmax:
                continue
            ev = {"declared_km2": round(d, 1), "polygon_km2": round(pa, 1), "ratio": round(r, 3)}
            sev, extra = "MEDIUM", ""
            # the polygon the point falls in, when it lies beyond the edge tolerance (an edge-case point says
            # nothing about which polygon the row's fields came from); its unit's whole polygon when it has one
            o = outside.get(u) or {}
            alt = o.get("in", []) if o.get("distance_km", 0) > tol else []
            if alt:
                alt_km2 = uarea[owner_of[alt[0]]] if alt[0] in owner_of else farea[alt[0]]
                rs = _ratio(d, alt_km2)
                ev.update(point_feature=alt[0], point_feature_km2=round(alt_km2, 1), ratio_swapped=round(rs, 3))
                if rs < r and rs <= rmax:
                    sev, extra = "HIGH", (f"; it matches feature {alt[0]}, where the point lies, better "
                                          f"(ratio {rs:.2f}): the row's geometry fields probably belong elsewhere")
            findings.append({"id": f"area_mismatch:{u}", "code": "area_mismatch", "severity": sev, "units": [u],
                             "evidence": ev,
                             "claim": f"unit {u}: declared area {d:,.0f} km2, polygon {pa:,.0f} km2 (ratio {r:.2f} "
                                      f"> {rmax:g}){extra}"})
    edge = [f for f in findings if f.get("edge")]
    counts = {"units": len(ids), "features": len(feats), "units_with_polygon": len(own), "units_with_point": len(pt),
              "points_checked": len(inside), "points_inside": sum(inside.values()), "edge_cases": len(edge),
              "reciprocal_swaps": sum(f["code"] == "reciprocal_swap" for f in findings),
              "points_outside": sum(f["code"] == "point_outside_own_polygon" and not f.get("edge") for f in findings),
              "area_checked": len([u for u in own if u not in swapped]) if areac else 0,
              "area_mismatches": sum(f["code"] == "area_mismatch" for f in findings),
              "area_mismatches_escalated": sum(f["code"] == "area_mismatch" and f["severity"] == "HIGH"
                                               for f in findings),
              "units_unmatched": sum(f["code"] == "unmatched" for f in findings)}
    checks = {"point": "ran", "reciprocal": "ran",
              "area": "ran" if areac else "not run: no area column declared", "unmatched": "ran"}
    return {"counts": counts, "findings": findings, "checks": checks}


def _sha(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _load_spec(run_dir: str):
    import yaml
    try:
        doc = yaml.safe_load(open(os.path.join(run_dir, SPEC_FILE), encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise GeometryError(f"{SPEC_FILE} unreadable: {e}") from e
    if not isinstance(doc, dict):
        raise GeometryError(f"{SPEC_FILE}: not a mapping")
    return doc


def _load_signoff(run_dir: str) -> dict | None:
    """The harness's geometry sign-off record, or None when the spec has not been signed with one. Unreadable: fail
    closed."""
    p = os.path.join(run_dir, SIGNOFF_FILE)
    if not os.path.exists(p):
        return None
    try:
        doc = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise GeometryError(f"{SIGNOFF_FILE}: unreadable geometry sign-off record: {e}") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("declaration"), dict):
        raise GeometryError(f"{SIGNOFF_FILE}: not a geometry sign-off record")
    return doc


def _spec_signed(run_dir: str) -> bool:
    """Is the decision spec signed? Its own sign-off record (toolkit/spec/signoff.py, agents/stage_machine.py), or
    the brief's when that hashed the spec (scripts/decision_brief.py). A brief-only sign-off is not."""
    for name in SPEC_SIGNOFFS:
        p = os.path.join(run_dir, name)
        if not os.path.exists(p):
            continue
        if name == "decision_spec.signoff.json":
            return True
        try:
            doc = json.load(open(p, encoding="utf-8"))
        except (OSError, ValueError):
            return True                   # unreadable: assume signed (fail closed: the record must then exist)
        if not isinstance(doc, dict) or (doc.get("sha256") or {}).get(SPEC_FILE) is not None:
            return True
    return False


def _declaration(cfg, pres, cols, units) -> dict:
    """The geometry part of the inputs, as declared (paths as written, so a moved run compares equal); labels are not
    part of it."""
    xw = (cfg or {}).get("crosswalk")
    return {"enabled": bool(cfg is not None and cols.get("lat") and cols.get("lon")), "presentation": pres,
            "boundaries": {"path": cfg["shown"], "feature_id": cfg["feature_id"]} if cfg else None,
            "crosswalk": {"path": xw["shown"], "unit_id": xw["unit_id"], "feature_id": xw["feature_id"],
                          "union": bool(xw.get("union"))} if xw else None,
            "columns": {k: cols.get(k) for k in ("id", "lat", "lon", "area")}, "units": units}


def _evidence(decl: dict, shas: dict) -> str:
    return hashlib.sha256(json.dumps({"declaration": decl, "sha256": shas}, sort_keys=True).encode()).hexdigest()


def _changes(signed: dict, decl: dict, shas: dict) -> list[str]:
    """What differs from the geometry frozen at sign-off (empty: nothing)."""
    old, out = signed.get("declaration") or {}, []
    if bool(old.get("enabled")) != decl["enabled"]:
        out.append("the check was " + ("enabled" if old.get("enabled") else "not enabled") + " at sign-off and is "
                   + ("enabled" if decl["enabled"] else "not enabled") + " now")
    for k in ("presentation", "boundaries", "crosswalk", "columns", "units"):
        if old.get(k) != decl.get(k):
            out.append(f"the {k} declaration changed ({old.get(k)} -> {decl.get(k)})")
    was = signed.get("input_sha256") or {}
    for k in ("boundaries", "crosswalk", "units"):
        if was.get(k) != shas.get(k):
            out.append(f"the {k} file changed (sha256 {str(was.get(k))[:12]} -> {str(shas.get(k))[:12]})")
    return out


def check(run_dir: str, spec: dict | None = None, th: dict | None = None, use_signoff: bool = True) -> dict:
    """The report for a run directory: status ran | not_run | failed, with reason, inputs (paths and sha256), the
    declaration and its evidence hash, the changes since the geometry sign-off, the thresholds, counts, findings
    (each with the spec's proposal and its acceptance) and acceptance problems. Never raises.

    Acceptance: a MEDIUM or LOW finding is accepted by the spec's entry with a reason. A HIGH one only by an owner
    approval in the harness's sign-off record whose evidence hash matches the current inputs."""
    rep = {"producer": PRODUCER, "version": 2, "status": "failed", "reason": None, "inputs": {}, "thresholds": None,
           "declaration": None, "evidence_sha256": None, "signoff": None, "signoff_changes": [],
           "counts": {}, "checks": {}, "findings": [], "acceptance_problems": []}
    try:
        spec = _load_spec(run_dir) if spec is None else spec
        du = spec.get("decision_units") if isinstance(spec, dict) else None
        if not isinstance(du, dict) or not isinstance(du.get("csv"), str) or not du.get("id"):
            raise GeometryError("decision_units: the spec names no units table and id column")
        acc = du.get(ACCEPT_KEY)
        if acc is not None and not (isinstance(acc, dict) and all(isinstance(k, str) and isinstance(v, str)
                                                                  and v.strip() for k, v in acc.items())):
            rep["acceptance_problems"].append(f"decision_units.{ACCEPT_KEY}: a mapping of finding id to a reason")
            acc = {}
        acc = acc or {}
        signed = _load_signoff(run_dir) if use_signoff else None
        rep["record_missing"] = bool(use_signoff and signed is None and _spec_signed(run_dir))
        cfg, pres = presentation_boundaries(run_dir)
        upath = du["csv"] if os.path.isabs(du["csv"]) else os.path.join(run_dir, du["csv"])
        rows = read_table(upath)
        header = list(rows[0]) if rows else []
        if not rows:
            raise GeometryError(f"{du['csv']}: the units table is empty")
        cols, bad = unit_columns(du, header)
        if bad:
            raise GeometryError("; ".join(bad))
        if cols["id"] not in header:
            raise GeometryError(f"{du['csv']}: no id column {cols['id']}")
        rep["inputs"] = {"units": {"path": du["csv"], "sha256": _sha(upath)}, "columns": cols,
                         "presentation": pres}
        decl = _declaration(cfg, pres, cols, du["csv"])
        shas = {"units": _sha(upath), "boundaries": _sha(cfg["path"]) if cfg else None,
                "crosswalk": _sha(cfg["crosswalk"]["path"]) if cfg and cfg.get("crosswalk") else None}
        ev_sha = _evidence(decl, shas)
        rep.update(declaration=decl, input_sha256=shas, evidence_sha256=ev_sha)
        approvals = {}
        if signed is not None:
            rep["signoff"] = {"signed_by": signed.get("signed_by"), "at": signed.get("at")}
            rep["signoff_changes"] = _changes(signed, decl, shas)
            approvals = {k: a for k, a in (signed.get("approvals") or {}).items()
                         if isinstance(a, dict) and a.get("approved_by") == "owner"
                         and a.get("evidence_sha256") == ev_sha and str(a.get("reason") or "").strip()}
        why = []
        if not (cols["lat"] and cols["lon"]):
            why.append("no coordinates declared (decision_units.coordinates {lat, lon}, or lat/lon columns)")
        if cfg is None:
            why.append("no boundaries declared (presentation.yaml boundaries: {path, feature_id, crosswalk})")
        if why:
            rep.update(status="not_run", reason="not run: " + "; ".join(why))
            rep["acceptance_problems"] += [f"decision_units.{ACCEPT_KEY}: {k} names no current finding (the check "
                                           "did not run)" for k in sorted(acc)]
            return rep
        th = th or thresholds()
        rep["thresholds"] = th
        feats = read_features(cfg)
        xw = read_crosswalk(cfg["crosswalk"]) if cfg.get("crosswalk") else None
        xw = resolve_targets(xw, [str(r.get(cols["id"], "")).strip() for r in rows],
                             bool((cfg.get("crosswalk") or {}).get("union")),
                             (cfg.get("crosswalk") or {}).get("shown") or cfg["shown"], feats)
        rep["inputs"]["boundaries"] = {"path": cfg["shown"], "feature_id": cfg["feature_id"],
                                       "sha256": shas["boundaries"]}
        if cfg.get("crosswalk"):
            c = cfg["crosswalk"]
            rep["inputs"]["crosswalk"] = {"path": c["shown"], "unit_id": c["unit_id"], "feature_id": c["feature_id"],
                                          "sha256": shas["crosswalk"]}
        ev = evaluate(rows, cols, feats, xw, th)
        ids = {f["id"] for f in ev["findings"]}
        for f in ev["findings"]:
            f["proposed"] = acc.get(f["id"])
            if f["severity"] == "HIGH":
                a = approvals.get(f["id"])
                f["accepted"] = a["reason"] if a else None
                f["approved_by"] = "owner" if a else None
            else:
                f["accepted"] = f["proposed"]
        rep["acceptance_problems"] += [f"decision_units.{ACCEPT_KEY}: {k} names no current finding" for k in sorted(acc)
                                       if k not in ids]
        rep.update(status="ran", counts=ev["counts"], checks=ev["checks"], findings=ev["findings"])
    except GeometryError as e:
        rep.update(status="failed", reason=str(e))
    except Exception as e:  # noqa: BLE001 -- fail closed: an unexpected input is a finding, not a crash
        rep.update(status="failed", reason=f"{type(e).__name__}: {e}")
    return rep


def write(run_dir: str, rep: dict, rel: str = ARTIFACT) -> str:
    p = os.path.join(run_dir, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=1, sort_keys=True)
    os.replace(tmp, p)
    return p


def run(run_dir: str, spec: dict | None = None) -> dict:
    """check() and write the artifact (harness-owned: data/input_checks.json)."""
    rep = check(run_dir, spec)
    write(run_dir, rep)
    return rep


def freeze(run_dir: str, by: str, extra: dict | None = None, spec: dict | None = None, th: dict | None = None) -> dict:
    """The sign-off's geometry record (data/geometry_signoff.json): the declaration and input hashes as signed, and,
    when the owner signs, an approval of each HIGH finding the spec proposes as an exception (plus `extra`
    {finding id: reason}), bound to the evidence hash. A lead sign-off approves nothing. A check that failed cannot be
    signed over (ValueError)."""
    import datetime as _dt
    if by not in ("owner", "lead"):
        raise ValueError(f"sign-off by {by!r}: owner or lead")
    rep = check(run_dir, spec, th, use_signoff=False)
    if rep["status"] == "failed":
        raise ValueError(f"the input geometry check failed: {rep['reason']}")
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    approvals = {}
    if by == "owner":
        highs = {f["id"]: f for f in rep["findings"] if f["severity"] == "HIGH"}
        want = {k: f["proposed"] for k, f in highs.items() if f.get("proposed")}
        for k, why in (extra or {}).items():
            if k not in highs or not str(why or "").strip():
                raise ValueError(f"approve {k}: not a current HIGH finding, or no reason")
            want[k] = str(why)
        approvals = {k: {"reason": why, "evidence_sha256": rep["evidence_sha256"], "severity": "HIGH",
                         "approved_by": "owner", "at": now} for k, why in sorted(want.items())}
    rec = {"producer": PRODUCER, "version": 1, "signed_by": by, "at": now, "declaration": rep["declaration"],
           "input_sha256": rep["input_sha256"], "evidence_sha256": rep["evidence_sha256"], "approvals": approvals}
    write(run_dir, rec, SIGNOFF_FILE)
    return rec


def approve(run_dir: str, by: str, extra: dict | None = None) -> dict:
    """The owner's command after sign-off: re-freeze the current geometry inputs and approve the spec's proposed HIGH
    exceptions (and `extra`). Only the owner (ValueError otherwise)."""
    if by != "owner":
        raise ValueError("only the owner approves a HIGH geometry exception")
    return freeze(run_dir, "owner", extra)


def owner_pending(rep: dict) -> list[str]:
    """HIGH findings the spec proposes as exceptions that the owner has not approved: the run waits for the owner."""
    return [f["id"] for f in rep.get("findings") or [] if f["severity"] == "HIGH" and f.get("proposed")
            and not f.get("accepted")]


_FIX_HINT = (f"fix the data (the derivation, the crosswalk or the boundary file), or propose it as an exception in "
             f"decision_units.{ACCEPT_KEY} with a reason: only the owner's sign-off approves a HIGH exception")
_OWNER_HINT = ("proposed as an exception; it waits for the owner's approval (the owner's sign-off, or "
               "`python3 toolkit/spec/geometry.py approve <run_dir> --by owner` after it)")


def problems(rep: dict) -> list[dict]:
    """What sends the spec back before SIGN: [{code, claim}] (the derivation_problems shape). A HIGH the spec proposes
    as an exception is not one: it goes to the owner (owner_pending)."""
    out = []
    if rep.get("status") == "failed":
        out.append({"code": FAILED, "claim": f"the input geometry check could not run on its declared inputs: "
                                             f"{rep.get('reason')}"})
    out += [{"code": ACCEPT_BAD, "claim": p} for p in rep.get("acceptance_problems") or []]
    if rep.get("record_missing"):
        out.append({"code": MISSING, "claim": "the decision spec is signed but there is no geometry sign-off record "
                                              f"({SIGNOFF_FILE}): it was signed before the check existed, or through a "
                                              "path that skipped it, so its geometry inputs are not frozen. The owner "
                                              "runs `python3 toolkit/spec/geometry.py approve <run_dir> --by owner` "
                                              "to freeze them"})
    out += [{"code": CHANGED, "claim": f"the geometry inputs changed after sign-off: {c}; the owner re-approves them "
                                       f"(`python3 toolkit/spec/geometry.py approve <run_dir> --by owner`) or restores "
                                       f"them"} for c in rep.get("signoff_changes") or []]
    out += [{"code": f["code"], "claim": f"{f['claim']} [{f['id']}]: {_FIX_HINT}"}
            for f in rep.get("findings") or [] if f.get("severity") == "HIGH" and not f.get("accepted")
            and not f.get("proposed")]
    return out


def findings(rep: dict) -> list[dict]:
    """The gate's view: a failed check, an invalid proposal, a change since sign-off and every HIGH the owner has not
    approved (HIGH, not declarable); unaccepted MEDIUM (declarable); LOW notes for a check that did not run and for
    edge cases. Stage PLAN, as the derivation findings."""
    base = {"stage": "PLAN", "target_stage": "PLAN"}
    out = [dict(base, kind=p["code"], severity="HIGH", scope_declarable=False, key=p["code"], claim=p["claim"])
           for p in problems(rep) if p["code"] in (FAILED, ACCEPT_BAD, CHANGED, MISSING)]
    for f in rep.get("findings") or []:
        if f.get("accepted") or f["severity"] not in ("HIGH", "MEDIUM"):
            continue
        tail = f"; {_OWNER_HINT}" if f["severity"] == "HIGH" and f.get("proposed") else ""
        out.append(dict(base, kind=f["code"], severity=f["severity"], scope_declarable=f["severity"] != "HIGH",
                        key=f["id"][:60], claim=f"{f['claim']} [{f['id']}]{tail}"))
    if rep.get("status") == "not_run":
        out.append(dict(base, kind=NOT_RUN, severity="LOW", scope_declarable=True, key="geometry",
                        claim=f"input geometry check {rep.get('reason')}: the units' coordinates and areas were "
                              "not checked against boundaries"))
    edge = [f for f in rep.get("findings") or [] if f.get("edge")]
    if edge:
        out.append(dict(base, kind=EDGE, severity="LOW", scope_declarable=True, key="geometry_edge",
                        claim=f"{len(edge)} unit point(s) lie just outside their own polygon, within the "
                              f"{(rep.get('thresholds') or {}).get('edge_tolerance_km')} km edge tolerance: "
                              + ", ".join(f["units"][0] for f in edge[:12]) + (" ..." if len(edge) > 12 else "")))
    return out


def sheet_lines(run_dir: str) -> list[str]:
    """The sign-off sheet's section, from the artifact the harness wrote."""
    out = ["## Input geometry check"]
    try:
        rep = json.load(open(os.path.join(run_dir, ARTIFACT), encoding="utf-8"))
    except (OSError, ValueError):
        return out + [f"No record ({ARTIFACT} is missing): the check has not run.", ""]
    st = rep.get("status")
    if st == "not_run":
        return out + [f"The check did not run: {rep.get('reason')}. Signing freezes that: declaring boundaries "
                      "later needs the owner.", ""]
    if st == "failed":
        return out + [f"The check failed: {rep.get('reason')}.", ""]
    c = rep.get("counts") or {}
    out.append(f"Checked {c.get('points_checked')} unit point(s) against {c.get('features')} boundary feature(s): "
               f"{c.get('reciprocal_swaps')} reciprocal swap(s), {c.get('points_outside')} point(s) outside their "
               f"polygon beyond the tolerance, {c.get('edge_cases')} edge case(s), {c.get('area_mismatches')} area "
               f"mismatch(es), {c.get('units_unmatched')} unmatched unit(s). Area check: {(rep.get('checks') or {}).get('area')}. Signing freezes the "
               "boundary declaration and the boundary and crosswalk files (sha256); a later change needs the owner.")
    fs = rep.get("findings") or []
    prop = [f for f in fs if f["severity"] == "HIGH" and f.get("proposed") and not f.get("accepted")]
    out += ["", "Proposed exceptions to HIGH findings (the owner's sign-off approves them; a lead sign-off does not):"]
    out += [f"- **{f['id']}**: {f['claim']}. Reason given: {f['proposed']}" for f in prop] or ["- none"]
    appr = [f for f in fs if f["severity"] == "HIGH" and f.get("accepted")]
    if appr:
        out += ["", "Approved by the owner:"] + [f"- **{f['id']}**: {f['claim']}. Reason: {f['accepted']}"
                                                 for f in appr]
    acc = [f for f in fs if f["severity"] != "HIGH" and f.get("accepted")]
    out += ["", "Accepted by name in the spec (MEDIUM and LOW):"]
    out += [f"- **{f['id']}** ({f['severity']}): {f['claim']}. Reason: {f['accepted']}" for f in acc] or ["- none"]
    opn = [f for f in fs if not f.get("accepted") and not f.get("proposed") and f["severity"] in ("HIGH", "MEDIUM")]
    if opn:
        out += ["", "Open (MEDIUM findings stay open at the gate):"]
        out += [f"- {f['id']} ({f['severity']}): {f['claim']}" for f in opn]
    return out + [""]


# --------------------------------------------------------------------------- CLI
def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("check", allow_abbrev=False)
    c.add_argument("run_dir")
    t = sub.add_parser("table", allow_abbrev=False)
    for k in ("boundaries", "feature_id", "units", "id", "lat", "lon"):
        t.add_argument("--" + k.replace("_", "-"), required=True)
    for k in ("crosswalk", "xw_unit", "xw_feature", "area"):
        t.add_argument("--" + k.replace("_", "-"))
    t.add_argument("--union", action="store_true")
    p = sub.add_parser("approve", help="the owner: re-freeze the current geometry inputs and approve the spec's "
                                       "proposed HIGH exceptions (and --accept ID=REASON)", allow_abbrev=False)
    p.add_argument("run_dir")
    p.add_argument("--by", choices=["owner"], required=True)
    p.add_argument("--accept", action="append", default=[], metavar="ID=REASON")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if a.cmd == "approve":
        extra = dict(x.split("=", 1) for x in a.accept if "=" in x)
        try:
            rec = approve(a.run_dir, a.by, extra)
        except ValueError as e:
            print(f"refused: {e}")
            return 1
        print(json.dumps({"approvals": sorted(rec["approvals"]), "evidence_sha256": rec["evidence_sha256"]}))
        run(a.run_dir)
        return 0
    if a.cmd == "check":
        rep = check(a.run_dir)          # prints only: data/input_checks.json is the harness's (Machine)
    elif a.cmd == "table":
        try:
            feats = read_features({"path": a.boundaries, "feature_id": a.feature_id})
            xw = read_crosswalk({"path": a.crosswalk, "unit_id": a.xw_unit, "feature_id": a.xw_feature}) \
                if a.crosswalk else None
            rows = read_table(a.units)
            xw = resolve_targets(xw, [str(r.get(a.id, "")).strip() for r in rows], a.union, a.crosswalk or a.boundaries,
                                 feats)
            ev = evaluate(rows, {"id": a.id, "lat": a.lat, "lon": a.lon, "area": a.area}, feats, xw, thresholds())
            rep = {"status": "ran", **ev}
        except GeometryError as e:
            rep = {"status": "failed", "reason": str(e)}
    else:
        ap.print_help()
        return 2
    print(json.dumps({k: rep.get(k) for k in ("status", "reason", "counts", "checks")}, indent=1))
    return 0 if rep.get("status") == "ran" else 1


# --------------------------------------------------------------------------- self-test
def _square(x0, y0, s):
    return [[x0, y0], [x0 + s, y0], [x0 + s, y0 + s], [x0, y0 + s], [x0, y0]]


def toy_inputs(d: str, area_col: bool = True) -> dict:
    """A synthetic run dir: squares of 0.5 degrees near 10N and a units table with one clean unit, one edge-case point
    just outside its square, one reciprocal swapped pair (points and areas exchanged), one area mismatch, one unit
    whose point and area belong to another unit's square (not reciprocal), one unit with no crosswalk row,
    a MultiPolygon unit (point in its second part) and a unit with a hole (point in the
    hole). Returns {name: unit id}."""
    import yaml
    s = 0.5
    sq = {f"F{k}": (k * 0.6, 10.0) for k in range(10)}        # 0.1 degree gaps between squares
    feats = []
    for fid, (x, y) in sq.items():
        if fid == "F7":                                       # a MultiPolygon: two squares, the second north
            g = {"type": "MultiPolygon", "coordinates": [[_square(x, y, s)], [_square(x, y + 0.6, s)]]}
        elif fid == "F8":                                     # a polygon with a hole in the middle
            g = {"type": "Polygon", "coordinates": [_square(x, y, s), _square(x + 0.15, y + 0.15, 0.2)]}
        else:
            g = {"type": "Polygon", "coordinates": [_square(x, y, s)]}
        feats.append({"type": "Feature", "properties": {"code": fid, "name": fid.lower()}, "geometry": g})
    with open(os.path.join(d, "b.geojson"), "w") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)
    A = area_km2([[_ring(_square(0, 10.0, s), 0, "t")]])
    c = {k: (x + s / 2, y + s / 2) for k, (x, y) in sq.items()}
    edge_lon = sq["F1"][0] + s + 0.02                          # ~2.2 km east of F1, in the gap
    rows = [("clean", "F0", c["F0"], A), ("edge", "F1", (edge_lon, c["F1"][1]), A),
            ("swap_a", "F2", c["F3"], A * 2.5), ("swap_b", "F3", c["F2"], A),
            ("big", "F4", c["F4"], A * 3), ("stray", "F5", c["F6"], A), ("owner6", "F6", c["F6"], A),
            ("nomatch", None, c["F9"], A),
            ("multi", "F7", (c["F7"][0], c["F7"][1] + 0.6), 2 * A), ("holed", "F8", c["F8"], A * (1 - 0.16))]
    # swap_a's square is shrunk so the swapped areas tell: give F3 a 2.5x area by a taller polygon
    feats[3]["geometry"] = {"type": "Polygon", "coordinates": [[[1.8, 10.0], [2.3, 10.0], [2.3, 11.25], [1.8, 11.25],
                                                                [1.8, 10.0]]]}
    c3 = (2.05, 10.625)
    rows[2] = ("swap_a", "F2", c3, area_km2([[_ring(feats[3]["geometry"]["coordinates"][0], 0, "t")]]))
    rows[3] = ("swap_b", "F3", c["F2"], A)
    # stray: its point is in F6 and its declared area matches F6 (which is 2x F5): an escalated area mismatch
    feats[6]["geometry"] = {"type": "Polygon", "coordinates": [[[3.6, 10.0], [4.1, 10.0], [4.1, 11.0], [3.6, 11.0],
                                                                [3.6, 10.0]]]}
    A6 = area_km2([[_ring(feats[6]["geometry"]["coordinates"][0], 0, "t")]])
    rows[5] = ("stray", "F5", (3.85, 10.75), A6)
    rows[6] = ("owner6", "F6", (3.85, 10.25), A6)
    with open(os.path.join(d, "b.geojson"), "w") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)
    with open(os.path.join(d, "units.csv"), "w") as f:
        f.write("uid,region,pop,x_lon,y_lat" + (",area_km2" if area_col else "") + "\n")
        for u, _, (x, y), a in rows:
            f.write(f"{u},r1,1000,{x:.5f},{y:.5f}" + (f",{a:.3f}" if area_col else "") + "\n")
    with open(os.path.join(d, "xw.csv"), "w") as f:
        f.write("uid,code\n" + "".join(f"{u},{fid}\n" for u, fid, _, _ in rows if fid))
    yaml.safe_dump({"boundaries": {"path": "b.geojson", "feature_id": "code", "credit": "toy (CC0)",
                                   "crosswalk": {"path": "xw.csv", "unit_id": "uid", "feature_id": "code"}}},
                   open(os.path.join(d, "presentation.yaml"), "w"))
    spec = {"decision_units": {"csv": "units.csv", "id": "uid", "region": "region",
                               "coordinates": {"lat": "y_lat", "lon": "x_lon", **({"area_km2": "area_km2"}
                                                                                   if area_col else {})}}}
    yaml.safe_dump(spec, open(os.path.join(d, SPEC_FILE), "w"))
    return {u: u for u, *_ in rows}


def _high_ids(rep) -> set:
    return {f["key"] for f in findings(rep) if f["severity"] == "HIGH"}


def _self_test_owner(ok, th) -> None:
    """IG8 (review P1): a HIGH exception is the owner's alone. The spec's entries are proposals; a HIGH one counts
    only once the owner's sign-off (or `approve --by owner`) has recorded it, bound to the current inputs. IG9
    (review P2): the sign-off freezes the geometry declaration and the boundary and crosswalk hashes; any later
    change is HIGH, and an approval lapses when its evidence changes. Labels stay unsigned."""
    import tempfile
    import yaml
    with tempfile.TemporaryDirectory() as d:
        toy_inputs(d)
        sp = yaml.safe_load(open(os.path.join(d, SPEC_FILE)))
        highs = ["reciprocal_swap:swap_a+swap_b", "point_outside_own_polygon:stray",
                 "area_mismatch:stray", "point_outside_own_polygon:holed"]
        sp["decision_units"][ACCEPT_KEY] = {k: "the planner says it is fine" for k in highs}
        sp["decision_units"][ACCEPT_KEY]["area_mismatch:big"] = "known boundary vintage difference"
        yaml.safe_dump(sp, open(os.path.join(d, SPEC_FILE), "w"))
        r = check(d, th=th)
        ok(problems(r) == [] and set(owner_pending(r)) == set(highs) and set(highs) <= _high_ids(r),
           f"IG8a a HIGH the spec proposes as an exception is not accepted: it stays HIGH at the gate and waits for the "
           f"owner, without sending the spec back ({sorted(_high_ids(r))}, {owner_pending(r)})")
        ok("area_mismatch:big" not in {f["key"] for f in findings(r)},
           "IG8b a MEDIUM stays agent-declarable: the spec's entry with a reason clears it")
        freeze(d, "lead", th=th)
        r = check(d, th=th)
        ok(set(highs) <= _high_ids(r) and set(owner_pending(r)) == set(highs),
           f"IG8c a lead sign-off approves nothing: the HIGH findings stay ({sorted(_high_ids(r))})")
        try:
            approve(d, "lead")
            ok(False, "IG8d only the owner approves a HIGH exception")
        except ValueError:
            ok(True, "IG8d only the owner approves a HIGH exception")
        rec = freeze(d, "owner", th=th)
        r = check(d, th=th)
        sw = next(f for f in r["findings"] if f["id"] == highs[0])
        ok(_high_ids(r) == set() and owner_pending(r) == [] and sw.get("approved_by") == "owner"
           and sorted(rec.get("approvals") or {}) == sorted(highs),
           f"IG8e the owner's sign-off records each proposed HIGH exception, bound to the inputs: none is left open "
           f"({sorted(_high_ids(r))})")
        # IG9 the frozen declaration
        pres = yaml.safe_load(open(os.path.join(d, "presentation.yaml")))
        yaml.safe_dump(dict(pres, labels={"units_title": "Units"}), open(os.path.join(d, "presentation.yaml"), "w"))
        r = check(d, th=th)
        ok(_high_ids(r) == set(), f"IG9a labels stay unsigned: a label edit changes nothing ({sorted(_high_ids(r))})")
        g = json.load(open(os.path.join(d, "b.geojson")))
        for f in g["features"]:                       # repoint to polygons whose two ids are exchanged
            f["properties"]["code"] = {"F2": "F3", "F3": "F2"}.get(f["properties"]["code"], f["properties"]["code"])
        json.dump(g, open(os.path.join(d, "b2.geojson"), "w"))
        yaml.safe_dump(dict(pres, boundaries=dict(pres["boundaries"], path="b2.geojson")),
                       open(os.path.join(d, "presentation.yaml"), "w"))
        r = check(d, th=th)
        ok(CHANGED in {f["kind"] for f in findings(r) if f["severity"] == "HIGH"}
           and CHANGED in {p["code"] for p in problems(r)},
           f"IG9b repointing the boundaries after sign-off (swapped ids make the planted swap pass) is a HIGH change "
           f"({[f['kind'] for f in findings(r)][:6]})")
        yaml.safe_dump({k: v for k, v in pres.items() if k != "boundaries"},
                       open(os.path.join(d, "presentation.yaml"), "w"))
        r = check(d, th=th)
        ok(r["status"] == "not_run" and CHANGED in {f["kind"] for f in findings(r) if f["severity"] == "HIGH"},
           f"IG9c removing the boundaries after sign-off is a HIGH change, not only a LOW note "
           f"({[(f['kind'], f['severity']) for f in findings(r)]})")
        yaml.safe_dump(pres, open(os.path.join(d, "presentation.yaml"), "w"))
        g = json.load(open(os.path.join(d, "b.geojson")))
        g["features"][0]["properties"]["note"] = "edited"
        json.dump(g, open(os.path.join(d, "b.geojson"), "w"))
        r = check(d, th=th)
        sw = next(f for f in r["findings"] if f["id"] == highs[0])
        ok(CHANGED in {f["kind"] for f in findings(r)} and not sw.get("accepted") and highs[0] in _high_ids(r),
           f"IG9d an edit of the boundary file after sign-off is a HIGH change, and the owner's approval lapses with "
           f"its evidence ({sw.get('accepted')})")
        approve(d, "owner")
        r = check(d, th=th)
        ok(_high_ids(r) == set(), f"IG9e the owner's approve command re-freezes the current inputs and approves the "
                                  f"proposals again ({sorted(_high_ids(r))})")
        open(os.path.join(d, SIGNOFF_FILE), "w").write("{oops")
        r = check(d, th=th)
        ok(r["status"] == "failed" and FAILED in {f["kind"] for f in findings(r)},
           "IG9f an unreadable geometry sign-off record fails closed")
    # IG13 (Codex round 2): a signed spec with no geometry record is HIGH (signed before the check, or through a path
    # that skipped the freeze); the owner's approve creates the record (the migration path)
    for how in ("spec", "brief"):
        with tempfile.TemporaryDirectory() as d:
            toy_inputs(d)
            r0 = check(d, th=th)
            if how == "spec":
                json.dump({"signed_by": "owner", "sha256": {SPEC_FILE: _sha(os.path.join(d, SPEC_FILE))}},
                          open(os.path.join(d, "decision_spec.signoff.json"), "w"))
            else:
                json.dump({"signed_by": "owner", "sha256": {SPEC_FILE: _sha(os.path.join(d, SPEC_FILE)),
                                                            "decision_brief.yaml": None}},
                          open(os.path.join(d, "decision_brief.signoff.json"), "w"))
            r1 = check(d, th=th)
            k1 = {(f["kind"], f["severity"], f["scope_declarable"]) for f in findings(r1)}
            try:
                approve(d, "owner")
                k2 = {f["kind"] for f in findings(check(d, th=th))}
            except (NameError, ValueError) as e:
                k2 = {f"error: {e}"}
            ok(MISSING not in {f["kind"] for f in findings(r0)} and (MISSING, "HIGH", False) in k1
               and MISSING not in k2,
               f"IG13 ({how}): a signed spec without a geometry record is HIGH {MISSING}, not declarable; the owner's "
               f"approve creates the record ({sorted(k1)[:3]}; after: {sorted(k2)[:3]})")
    with tempfile.TemporaryDirectory() as d:
        toy_inputs(d)
        os.remove(os.path.join(d, SPEC_FILE))
        json.dump({"signed_by": "owner", "sha256": {"decision_brief.yaml": "x", SPEC_FILE: None}},
                  open(os.path.join(d, "decision_brief.signoff.json"), "w"))
        ok(not _spec_signed(d), "IG13b a brief-only sign-off (no spec hashed) is not a signed spec")


def _self_test_shapes(ok, th) -> None:
    """IG10 (review P2): a degenerate ring fails closed. IG11: overlapping or nested polygons do not crash the
    reciprocal test. IG12: areas by equal-area integration of edges straight in longitude/latitude: closed forms,
    and resampling an edge with collinear points leaves the area unchanged."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        bad = {"collinear": [[0, 0], [1, 1], [2, 2], [0, 0]], "two distinct": [[0, 0], [1, 0], [1, 0], [1, 0], [0, 0]],
               "zero area": [[0, 0], [1, 0], [2, 0], [1, 0], [0, 0]]}
        got = {}
        for name, ring in bad.items():
            for hole in (False, True):
                coords = [_square(-5, -5, 20), ring] if hole else [ring]
                json.dump({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"code": "F0"},
                                                                       "geometry": {"type": "Polygon",
                                                                                    "coordinates": coords}}]},
                          open(os.path.join(d, "g.geojson"), "w"))
                try:
                    read_features({"path": os.path.join(d, "g.geojson"), "feature_id": "code"})
                    got[(name, hole)] = "read"
                except GeometryError:
                    got[(name, hole)] = "failed"
        ok(all(v == "failed" for v in got.values()),
           f"IG10 a ring without 3 distinct non-collinear vertices or with no area fails closed, as an outer ring or "
           f"a hole ({got})")
    rect = lambda x0, y0, x1, y1: [[_ring([[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]], 0, "t")]]  # noqa: E731
    feats = {"A": rect(0.5, 0, 1.5, 1), "B": rect(0, 0, 2, 1)}              # A nested inside B
    rows = [{"u": "a", "x": 1.8, "y": 0.5}, {"u": "b", "x": 1.0, "y": 0.5}]  # a's point in B; b's point in A and B
    try:
        ev = evaluate(rows, {"id": "u", "lat": "y", "lon": "x"}, feats, {"a": "A", "b": "B"}, th)
        codes = sorted((f["code"], f["severity"]) for f in ev["findings"])
        ok(codes == [("point_outside_own_polygon", "HIGH")],
           f"IG11 nested polygons: a pair forms only when both points lie outside their own polygons ({codes})")
    except Exception as e:  # noqa: BLE001
        ok(False, f"IG11 nested polygons: a pair forms only when both points lie outside their own polygons: "
                  f"{type(e).__name__}: {e}")
    # IG14 (the one loader for figures and check, merged from #127): crosswalk union and its refusals, unique feature
    # ids, an empty MultiPolygon, and the geographic-CRS rule
    import yaml
    with tempfile.TemporaryDirectory() as d:
        def run_with(xw_rows=None, union=None, edit=None, crs=None):
            toy_inputs(d)
            if xw_rows is not None:
                open(os.path.join(d, "xw.csv"), "w").write("uid,code\n" + "".join(f"{u},{f}\n" for u, f in xw_rows))
            if union is not None:
                p = yaml.safe_load(open(os.path.join(d, "presentation.yaml")))
                p["boundaries"]["crosswalk"]["union"] = union
                yaml.safe_dump(p, open(os.path.join(d, "presentation.yaml"), "w"))
            if edit or crs:
                g = json.load(open(os.path.join(d, "b.geojson")))
                if edit:
                    edit(g["features"])
                if crs:
                    g["crs"] = {"type": "name", "properties": {"name": crs}}
                json.dump(g, open(os.path.join(d, "b.geojson"), "w"))
            return check(d, th=th)
        base = [(u, f) for u, f in (("clean", "F0"), ("edge", "F1"), ("swap_a", "F2"), ("swap_b", "F3"), ("big", "F4"),
                                    ("stray", "F5"), ("owner6", "F6"), ("multi", "F7"), ("holed", "F8"))]
        r_multi = run_with(base + [("clean", "F9")])
        r_union = run_with(base + [("clean", "F9")], union=True)
        r_shared = run_with(base + [("nomatch", "F0")])
        r_dup = run_with(edit=lambda fs: fs[9]["properties"].update(code="F0"))
        r_empty = run_with(edit=lambda fs: fs[4].update(geometry={"type": "MultiPolygon", "coordinates": []}))
        r_crs = run_with(crs="EPSG:3857")
        r_crs84 = run_with(crs="urn:ogc:def:crs:OGC:1.3:CRS84")
        cl = [f["id"] for f in r_union.get("findings") or [] if "clean" in f["units"]]
        ok(r_multi["status"] == "failed" and "clean" in r_multi["reason"] and "union" in r_multi["reason"]
           and r_union["status"] == "ran" and cl == ["area_mismatch:clean"],
           f"IG14a a unit mapped to two features is refused (naming it) unless crosswalk.union: true; with union it is "
           f"checked against the union (its point is inside, its area is now half the union's) "
           f"({r_multi['reason'][:80]}; {cl})")
        ok(r_shared["status"] == "failed" and "clean" in r_shared["reason"] and "nomatch" in r_shared["reason"],
           f"IG14b a feature mapped to two units is refused, naming both ({r_shared['reason'][:100]})")
        ok(r_dup["status"] == "failed" and "not unique" in r_dup["reason"],
           f"IG14c a feature id used by two features is refused ({r_dup['reason'][:80]})")
        ok(r_empty["status"] == "ran" and any(f["id"] == "unmatched:big" for f in r_empty["findings"]),
           f"IG14d a unit whose feature is an empty MultiPolygon is unmatched ({r_empty['reason']})")
        ok(r_crs["status"] == "failed" and "CRS" in r_crs["reason"] and r_crs84["status"] == "ran",
           f"IG14e a projected CRS is refused; CRS84 is accepted ({r_crs['reason'][:80]})")
    # IG15 (focused check of #131, P2): union members must not overlap, or the union's area would count the overlap
    # twice (two rectangles whose union is 1.2 square degrees counted as 2.0); touching members are fine
    sets = {"overlapping": {"A": rect(0, 0, 1, 1), "B": rect(0.2, 0, 1.2, 1)},
            "nested": {"A": rect(0, 0, 1, 1), "B": rect(0.2, 0.2, 0.4, 0.4)},
            "identical": {"A": rect(0, 0, 1, 1), "B": rect(0, 0, 1, 1)},
            "touching": {"A": rect(0, 0, 1, 1), "B": rect(1, 0, 2, 1)},
            "apart": {"A": rect(0, 0, 1, 1), "B": rect(3, 0, 4, 1)}}
    got15 = {}
    for name, fs in sets.items():
        try:
            resolve_targets({"u": ["A", "B"]}, ["u"], True, "xw.csv", fs)
            got15[name] = "ok"
        except GeometryError as e:
            got15[name] = "refused" if "u" in str(e) and "overlap" in str(e) else f"refused: {e}"
        except Exception as e:  # noqa: BLE001 -- before the rule exists (alpha)
            got15[name] = f"{type(e).__name__}"
    ev15 = evaluate([{"u": "u", "x": "0.5", "y": "0.5", "a": str(2 * area_km2(rect(0, 0, 1, 1)))}],
                    {"id": "u", "lat": "y", "lon": "x", "area": "a"}, sets["touching"], {"u": ["A", "B"]}, th)
    ok(got15 == {"overlapping": "refused", "nested": "refused", "identical": "refused", "touching": "ok",
                 "apart": "ok"} and ev15["findings"] == [],
       f"IG15 union members that overlap (partly, nested or identical) are refused, naming the unit; touching or "
       f"separate members are allowed and their areas add up exactly ({got15}; {ev15['findings'][:1]})")
    R2 = 6371.0088 ** 2
    cell = rect(0, 10, 1, 11)
    exact = R2 * math.radians(1.0) * (math.sin(math.radians(11)) - math.sin(math.radians(10)))
    octant = [[_ring([[0, 0], [90, 0], [90, 90], [0, 90], [0, 0]], 0, "t")]]
    ok(abs(area_km2(cell) / exact - 1) < 1e-9 and abs(area_km2(octant) / (math.pi * R2 / 2) - 1) < 1e-9,
       f"IG12a closed forms: a 1-degree cell ({area_km2(cell):.4f} vs {exact:.4f}) and an octant of the sphere")
    big = [[0, 0], [10, 0], [10, 20], [0, 20], [0, 0]]
    dense = [[0, 0], [10, 0], [10, 20]] + [[10 - 10 * k / 50, 20] for k in range(1, 50)] + [[0, 20], [0, 0]]
    a1, a2 = area_km2([[_ring(big, 0, "t")]]), area_km2([[_ring(dense, 0, "t")]])
    tri = [[0, 0], [0.5, 0.1], [0.2, 0.6], [0, 0]]
    tri_d = [[0, 0]] + [[0.5 * k / 9, 0.1 * k / 9] for k in range(1, 10)] + [[0.2, 0.6], [0, 0]]
    t1, t2 = area_km2([[_ring(tri, 0, "t")]]), area_km2([[_ring(tri_d, 0, "t")]])
    ok(abs(a2 / a1 - 1) < 1e-9 and abs(t2 / t1 - 1) < 1e-9,
       f"IG12b resampling an edge with collinear points leaves the area unchanged ({a1:.1f} vs {a2:.1f} km2; "
       f"{t1:.4f} vs {t2:.4f} km2)")


def _self_test() -> int:
    import tempfile
    import yaml
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)
    th = {"edge_tolerance_km": 5.0, "area_ratio_max": 1.5}
    with tempfile.TemporaryDirectory() as d:
        # IG1 planar geometry: ray casting with holes and parts, distance and area against closed forms
        sq = [[_ring(_square(0, 10, 1.0), 0, "t")]]
        exact = (6371.0088 ** 2) * math.radians(1.0) * (math.sin(math.radians(11)) - math.sin(math.radians(10)))
        ok(abs(area_km2(sq) / exact - 1) < 1e-3, f"IG1a planar cos-latitude area within 0.1% of the spherical area "
                                                 f"for a 1-degree cell ({area_km2(sq):.1f} vs {exact:.1f})")
        holed = [[_ring(_square(0, 10, 1.0), 0, "t"), _ring(_square(0.25, 10.25, 0.5), 0, "t")]]
        ok(contains(holed, 0.1, 10.1) and not contains(holed, 0.5, 10.5) and not contains(holed, 1.5, 10.5)
           and abs(area_km2(holed) / (area_km2(sq) * 0.75) - 1) < 2e-3,
           "IG1b a point in a hole is outside; a polygon's area excludes its hole")
        multi = sq + [[_ring(_square(5, 10, 1.0), 0, "t")]]
        ok(contains(multi, 5.5, 10.5) and abs(area_km2(multi) / (2 * area_km2(sq)) - 1) < 1e-3,
           "IG1c a MultiPolygon contains a point in any part and sums its parts' areas")
        dk = distance_km(sq, 1.1, 10.5)
        ok(abs(dk - 0.1 * KM_PER_DEG * math.cos(math.radians(10.5))) < 0.05,
           f"IG1d distance to the boundary scales longitude by cos(latitude) ({dk:.2f} km)")

        # IG2 the synthetic run: one finding per planted error, and nothing on the clean unit
        toy_inputs(d)
        rep = check(d, th=th)
        by = {f["id"]: f for f in rep["findings"]}
        ok(rep["status"] == "ran" and not any("clean" in f["units"] for f in rep["findings"]),
           f"IG2a the check runs and the clean unit has no finding ({rep['status']}, {rep['reason']})")
        e = by.get("point_outside_own_polygon:edge") or {}
        ok(e.get("severity") == "LOW" and e.get("edge") and 1.0 < e["evidence"]["distance_km"] < 5.0,
           f"IG2b a point just outside its square is an edge case (LOW): {e.get('evidence')}")
        rows_e = [{"uid": "e", "x": 0.502, "y": 0.2, "a": area_km2([[_ring(_square(0.5, 0, 0.5), 0, "t")]])}]
        fe = {"P": [[_ring(_square(0.25, 0, 0.25), 0, "t")]], "Q": [[_ring(_square(0.5, 0, 0.5), 0, "t")]]}
        ev_e = evaluate(rows_e, {"id": "uid", "lat": "y", "lon": "x", "area": "a"}, fe, {"e": "P"}, th)
        ok([(f["code"], f["severity"]) for f in ev_e["findings"]] == [("point_outside_own_polygon", "LOW"),
                                                                       ("area_mismatch", "MEDIUM")],
           f"IG2b' an edge-case point inside a neighbour does not escalate the unit's area mismatch "
           f"({[(f['code'], f['severity']) for f in ev_e['findings']]})")
        s = by.get("reciprocal_swap:swap_a+swap_b") or {}
        ok(s.get("severity") == "HIGH" and s["evidence"].get("areas_consistent_with_swap") is True
           and not any(k.endswith(("swap_a", "swap_b")) for k in by if not k.startswith("reciprocal_swap")),
           f"IG2c the swapped pair is one HIGH reciprocal_swap whose areas match better swapped, with no separate "
           f"point or area finding: {s.get('evidence')}")
        b = by.get("area_mismatch:big") or {}
        ok(b.get("severity") == "MEDIUM" and abs(b["evidence"]["ratio"] - 3) < 0.01,
           f"IG2d a declared area 3x the polygon's is a MEDIUM area_mismatch: {b.get('evidence')}")
        st = by.get("point_outside_own_polygon:stray") or {}
        sa = by.get("area_mismatch:stray") or {}
        ok(st.get("severity") == "HIGH" and st["evidence"]["in_units"] == ["owner6"]
           and sa.get("severity") == "HIGH" and sa["evidence"]["ratio_swapped"] < 1.01,
           f"IG2e a point in another unit's polygon (not reciprocal) is HIGH, and its area mismatch escalates to HIGH "
           f"because it matches that polygon: {st.get('evidence')} {sa.get('evidence')}")
        ok((by.get("unmatched:nomatch") or {}).get("severity") == "MEDIUM",
           "IG2f a unit with no polygon is MEDIUM unmatched")
        ok(not any("multi" in f["units"] for f in rep["findings"]),
           "IG2g a MultiPolygon unit whose point is in its second part, with the summed area, has no finding")
        h = by.get("point_outside_own_polygon:holed") or {}
        ok(h.get("severity") == "HIGH" and h["evidence"]["in_features"] == [] and "area_mismatch:holed" not in by,
           f"IG2h a point in its polygon's hole is outside it (HIGH, in no polygon); the area net of the hole matches "
           f"({h.get('evidence')})")
        c = rep["counts"]
        ok((c["reciprocal_swaps"], c["edge_cases"], c["points_outside"], c["area_mismatches"],
            c["area_mismatches_escalated"], c["units_unmatched"]) == (1, 1, 2, 2, 1, 1),
           f"IG2i the counts: {c}")

        # IG3 pre-sign problems and the gate's view; acceptance by name
        pr = {p["code"] for p in problems(rep)}
        ok(pr == {"reciprocal_swap", "point_outside_own_polygon", "area_mismatch"}
           and all(p["code"] != "area_mismatch" or "stray" in p["claim"] for p in problems(rep)),
           f"IG3a every unaccepted HIGH blocks SIGN, and nothing below HIGH does ({sorted(pr)})")
        g = {(f["kind"], f["key"]): f for f in findings(rep)}
        ok(g[("reciprocal_swap", "reciprocal_swap:swap_a+swap_b")]["scope_declarable"] is False
           and g[("area_mismatch", "area_mismatch:big")]["scope_declarable"] is True
           and g[(EDGE, "geometry_edge")]["severity"] == "LOW" and all(f["stage"] == "PLAN" for f in g.values()),
           "IG3b at the gate: HIGH not declarable, MEDIUM declarable, edge cases one LOW note, stage PLAN")
        sp = yaml.safe_load(open(os.path.join(d, SPEC_FILE)))
        sp["decision_units"][ACCEPT_KEY] = {"reciprocal_swap:swap_a+swap_b": "checked by the owner: correct as is",
                                            "point_outside_own_polygon:stray": "the point is the main town",
                                            "area_mismatch:stray": "known boundary change",
                                            "point_outside_own_polygon:holed": "the point is the enclave's town"}
        rep2 = check(d, sp, th)
        ok(problems(rep2) == [] and len(owner_pending(rep2)) == 4 and not any(
            f.get("accepted") for f in rep2["findings"] if f["severity"] == "HIGH"),
           f"IG3c proposed by name with a reason, the spec is not sent back, and the HIGH findings wait for the owner "
           f"({problems(rep2)[:2]}, {owner_pending(rep2)})")
        sp["decision_units"][ACCEPT_KEY]["reciprocal_swap:x+y"] = "stale"
        ok([p["code"] for p in problems(check(d, sp, th))] == [ACCEPT_BAD],
           "IG3d an acceptance that names no current finding is itself a problem")
        sp["decision_units"][ACCEPT_KEY] = {"reciprocal_swap:swap_a+swap_b": ""}
        ok(ACCEPT_BAD in {p["code"] for p in problems(check(d, sp, th))},
           "IG3e an acceptance without a reason is refused")
        write(d, rep2)
        sh = "\n".join(sheet_lines(d))
        ok("reciprocal_swap:swap_a+swap_b" in sh and "checked by the owner" in sh and "Open" in sh,
           "IG3f the sign-off sheet lists each acceptance with its reason, and the open MEDIUM findings")

    # IG4 not run: never a silent pass
    with tempfile.TemporaryDirectory() as d:
        toy_inputs(d)
        os.remove(os.path.join(d, "presentation.yaml"))
        r = check(d, th=th)
        ok(r["status"] == "not_run" and "no boundaries declared" in r["reason"]
           and [f["kind"] for f in findings(r)] == [NOT_RUN] and problems(r) == [],
           f"IG4a no boundaries: not run, a LOW note at the gate, nothing blocks ({r['reason']})")
        toy_inputs(d)
        sp = yaml.safe_load(open(os.path.join(d, SPEC_FILE)))
        sp["decision_units"].pop("coordinates")
        r = check(d, sp, th)
        ok(r["status"] == "not_run" and "not run: no coordinates declared" in r["reason"],
           f"IG4b no declared coordinate columns (and none named lat/lon): not run: no coordinates declared "
           f"({r['reason']})")
        with open(os.path.join(d, "units.csv")) as f:
            txt = f.read().replace("x_lon,y_lat", "lon,lat", 1)
        open(os.path.join(d, "units.csv"), "w").write(txt)
        r = check(d, sp, th)
        ok(r["status"] == "ran" and r["inputs"]["columns"]["from"] == "column names",
           "IG4c columns named lat/lon are used when nothing is declared (the figures' convention)")
        toy_inputs(d, area_col=False)
        r = check(d, th=th)
        ok(r["status"] == "ran" and r["checks"]["area"].startswith("not run") and r["counts"]["area_mismatches"] == 0,
           f"IG4d no area column: the area check says not run; the point checks run ({r['checks']})")

    # IG5 fail closed on malformed inputs
    with tempfile.TemporaryDirectory() as d:
        bad_docs = {"not json": "{oops", "no features": json.dumps({"type": "FeatureCollection", "features": []}),
                    "no id": json.dumps({"type": "FeatureCollection", "features": [
                        {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon",
                                                                          "coordinates": [_square(0, 0, 1)]}}]}),
                    "line": json.dumps({"type": "FeatureCollection", "features": [
                        {"type": "Feature", "properties": {"code": "F0"},
                         "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}}]}),
                    "short ring": json.dumps({"type": "FeatureCollection", "features": [
                        {"type": "Feature", "properties": {"code": "F0"},
                         "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [0, 0]]]}}]}),
                    "projected": json.dumps({"type": "FeatureCollection", "features": [
                        {"type": "Feature", "properties": {"code": "F0"},
                         "geometry": {"type": "Polygon", "coordinates": [_square(500000, 1100000, 1000)]}}]}),
                    "nan": '{"type": "FeatureCollection", "features": [{"type": "Feature", "properties": '
                           '{"code": "F0"}, "geometry": {"type": "Polygon", "coordinates": '
                           '[[[0, 0], [1, 0], [NaN, 1], [0, 0]]]}}]}'}
        got = {}
        for name, txt in bad_docs.items():
            toy_inputs(d)
            open(os.path.join(d, "b.geojson"), "w").write(txt)
            r = check(d, th=th)
            got[name] = (r["status"], [p["code"] for p in problems(r)], [f["kind"] for f in findings(r)])
        ok(all(v == ("failed", [FAILED], [FAILED]) for v in got.values()),
           f"IG5a a malformed boundary file fails closed: a HIGH geometry_check_failed before SIGN and at the gate "
           f"({ {k: v[0] for k, v in got.items()} })")
        toy_inputs(d)
        open(os.path.join(d, "presentation.yaml"), "w").write("boundaries: {path: b.geojson}\n")
        r1 = check(d, th=th)
        toy_inputs(d)
        sp = yaml.safe_load(open(os.path.join(d, SPEC_FILE)))
        sp["decision_units"]["coordinates"]["lat"] = "missing_col"
        r2 = check(d, sp, th)
        toy_inputs(d)
        open(os.path.join(d, "xw.csv"), "w").write("uid,code\nclean,F0\nclean,F1\n")
        r3 = check(d, th=th)
        open(os.path.join(d, "labels.yaml"), "w").write("labels: {}\n")
        r4 = check(d, th=th)
        ok([x["status"] for x in (r1, r2, r3, r4)] == ["failed"] * 4,
           f"IG5b a boundaries block without feature_id, a declared column the table lacks, a crosswalk that maps a "
           f"unit twice and two presentation files all fail closed ({[x['reason'][:40] for x in (r1, r2, r3, r4)]})")
        try:
            thresholds(os.path.join(d, "presentation.yaml"))
            ok(False, "IG5c missing thresholds fail closed")
        except GeometryError:
            ok(True, "IG5c missing thresholds fail closed")
    t = thresholds()
    ok(t["edge_tolerance_km"] > 0 and t["area_ratio_max"] > 1,
       f"IG6 the thresholds come from effort_floors.yaml input_checks.geometry: {t}")
    for name, fn in (("IG8-IG9 owner approval and the frozen declaration", _self_test_owner),
                     ("IG10-IG12 degenerate rings, overlapping polygons, area", _self_test_shapes)):
        try:
            fn(ok, th)
        except Exception as e:  # noqa: BLE001 -- a missing function (step alpha) is a failure, not a crash
            ok(False, f"{name}: {type(e).__name__}: {e}")
    # IG7 disease-agnostic (CLAUDE.md): no disease, place or intervention words in this module's code
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import pipeline as P
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    ok(P.domain_terms(src) == [], f"IG7 the module names no disease, place or intervention: {P.domain_terms(src)}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
