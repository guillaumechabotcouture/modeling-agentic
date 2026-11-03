# v2: a standing check of the units' coordinates and areas against boundaries (design note)

**Status:** written before the code (2026-11-02). Built in branch `input-geometry-check`: tests (α), then code (β).
It extends `v2-derived-inputs.md` (the pre-sign checks and the sign-off sheet) and reuses the boundary
declaration from `v2-figures.md` (PR #127).

## 1. Evidence

In a real run, the geography input table had been built from a boundary set in which 4 pairs of unit names
were attached to each other's polygons. The table holds each unit's coordinates, area and population, plus
values computed over its polygon from rasters (such as prevalence estimates and rainfall). In each pair, each
unit carried its partner's coordinates, area and population. Its other attributes, joined by name from a
different source, were correct. Nothing in the harness caught this. It was found by hand, by checking each
unit's point against independent official boundaries: 4 reciprocal pairs, each unit's point inside the
partner's polygon, with areas consistent with a swap.

Every later step (calibration, allocation, report) trusted those rows. A mismatch like this one is
mechanical to detect once the boundaries are on hand, and the figures work (PR #127) already declares them.

## 2. Design

**Inputs.** No new declaration. The boundaries are the run's unsigned presentation file
(`presentation.yaml`, or `labels.yaml`; both present is an error), the same block the figures read:

```yaml
boundaries:
  path: boundaries/units.geojson         # GeoJSON FeatureCollection, Polygon / MultiPolygon, lon/lat degrees
  feature_id: <feature property>
  crosswalk: {path: boundaries/crosswalk.csv, unit_id: <units id column>, feature_id: <feature property>}
  credit: "<source and licence>"
```

Without a crosswalk, a unit id matches the feature property directly. The units table is the spec's
`decision_units.csv`. Its point and area columns are `decision_units.coordinates: {lat, lon, area_km2}`
(the figures' `{lat, lon}` plus an optional area key), or else columns named `lat`/`latitude`,
`lon`/`lng`/`long`/`longitude` and `area_km2` (the figures' naming rule). A declared column that the table
lacks is an error, not a fallback.

**Checks** (`toolkit/spec/geometry.py`). They use pure Python and json, with no new dependency.
Point-in-polygon uses ray casting (even-odd, so holes and MultiPolygon parts are handled). Distances are
computed on a plane scaled by cos(latitude) at the point. Areas use equal-area integration on the sphere,
R²·|∮ sin φ dλ|, integrated exactly along each edge taken as straight in longitude/latitude
(Δλ·sin φ̄·sinc(Δφ/2)). This is exact for a latitude/longitude cell and for an octant, and adding collinear
points to an edge leaves the area unchanged. (Review of #131: the first version scaled by the ring's mean
latitude, so densifying one edge changed the area.) A ring needs 3 distinct, non-collinear vertices and a
positive area; otherwise the result is `geometry_check_failed`. A reciprocal pair forms only when both points
lie outside their own polygons, so overlapping or nested polygons cannot make one.

| Code | Severity | When |
|---|---|---|
| `reciprocal_swap` | HIGH | A's point is in B's polygon and B's point is in A's. The two rows' geometry-derived fields are probably exchanged. Found at any distance, and reported once per pair, with the area ratios own vs swapped as evidence. |
| `point_outside_own_polygon` | HIGH | The point lies more than `edge_tolerance_km` outside its own polygon (the evidence names the polygon it falls in, if any). |
| | LOW | Within the tolerance: an edge case, listed but never blocking. |
| `area_mismatch` | MEDIUM | max(declared, polygon) / min(declared, polygon) > `area_ratio_max`, or there is no positive declared area. |
| | HIGH | The declared area matches better the polygon where the point lies (beyond the tolerance), within the factor: the row probably belongs to that polygon. |
| `unmatched` | MEDIUM | A unit with no polygon (no crosswalk row, or a feature missing from the file) or with no usable point. |
| | HIGH | One feature claimed by two or more units. |
| `geometry_check_failed` | HIGH | A declared input cannot be read or parsed: invalid JSON, no features, a feature without its id property, a geometry that is not a polygon, a ring with fewer than 3 positions, a non-finite or non-degree coordinate, a crosswalk that maps one unit to two features, or a declared column that is missing. |
| `geometry_check_not_run` | LOW | No boundaries declared, or no coordinates declared ("not run: no coordinates declared"). This is never a silent pass. |

**Thresholds.** These are in `.claude/orchestration/effort_floors.yaml` under a new section,
`input_checks.geometry`. They are not in `toolkit/analysis/thresholds.yaml`, because that file is hashed into
every benchmark's declaration digest (`toolkit/engines/benchmark.py declaration_digest`), and a key added there
would mark every stored benchmark stamp stale.
- `edge_tolerance_km: 5.0`. This covers the ways a correct point can end up just across a border: boundary
  simplification (vertex shifts of about 1-2 km in simplified boundary sets), coordinates rounded to 0.01
  degree (about 1.1 km), and reference points such as a main town placed near a border. A swapped row's point
  usually lies tens of km outside its own polygon, and reciprocal swaps are found at any distance.
- `area_ratio_max: 1.5`. Simplification changes polygon areas by a few percent, and boundary vintages change
  them by tens of percent. A factor of 1.5 tolerates both, and still flags a row carrying a neighbour's area
  when the two units differ in size by half or more.

**Output.** The harness writes `data/input_checks.json` (harness-owned: added to `HARNESS_FILES`, and no task
owns `data/`). It holds the status (ran, not_run or failed) with its reason, the inputs with their sha256, the
column names used and where they came from, the thresholds, the counts, and every finding with its id,
severity, units, claim, evidence and acceptance.

**Where it surfaces.** It follows the derivation checks (`input_column_unaccounted` and the others):
- *Before SIGN* (`h_SPEC`, both backends, after `pre_sign_problems`): each HIGH finding that the spec does not
  propose as an exception is a `code: claim` problem. The spec goes back to the planner with it, so it blocks
  signing. The problem text says how to clear it: fix the data (the derivation, the crosswalk or the boundary
  file), or propose an exception for the owner.
- *At SIGN* (`h_SIGN`): when a proposed HIGH exception has no owner approval, the run pauses at
  `awaiting_signoff`, as for a spec sign-off. With `--signoff lead` too, because a lead sign-off approves
  nothing.
- *At every gate* (`Machine.harness_findings`): the check re-runs on the current files. An unaccepted HIGH is a
  HIGH finding that cannot be declared; a MEDIUM is a declarable MEDIUM; the not-run note and the edge-case
  summary are LOW. All have stage PLAN. They reach `findings.jsonl` through the gate, as the derivation
  findings do, so the critics and the fix planner see them.
- *Sign-off sheet* (`decision_spec.signoff_sheet.md`): a section with the counts, each finding accepted by
  name with its reason, and the open findings, or the not-run or failed reason.

**Exceptions** (revised after the review of #131). `decision_units.geometry_accepted: {<finding id>:
"<reason>"}` in the spec holds *proposals*, which the planner may write. Finding ids are stable and readable:
`reciprocal_swap:<a>+<b>`, `point_outside_own_polygon:<u>`, `area_mismatch:<u>`, `unmatched:<u>`,
`unmatched:feature:<f>`.
- A MEDIUM or LOW proposal with a reason is accepted, as `defaults_accepted` is.
- A HIGH proposal counts only once the owner approves it. The approval is a harness record,
  `data/geometry_signoff.json`, which no task can write (it is in `HARNESS_FILES`). It is written by the owner's
  sign-off (`agents/stage_machine.py sign --by owner`, `toolkit/spec/signoff.py sign`), or after sign-off by
  `python3 toolkit/spec/geometry.py approve <run_dir> --by owner [--accept ID=REASON]`.
- Each approval is bound to the finding id and to the evidence hash: the sha256 of the declaration plus the
  boundary, crosswalk and units file hashes. When the evidence changes, the approval lapses.
- A lead sign-off approves nothing, and `approve` refuses anyone but the owner.
- A proposal with no reason, or one that names no current finding, is `geometry_acceptance_invalid` (HIGH).

**Freeze.** Every sign-off, by the lead or the owner, writes the geometry part of the inputs into that record:
- whether the check is enabled;
- the presentation file's name;
- the boundaries and crosswalk as declared (paths as written, feature and unit id fields);
- the columns used;
- the sha256 of the boundary, crosswalk and units files.

These sit alongside the spec and derivation hashes in the spec's own sign-off. Any later difference is
`geometry_inputs_changed_after_signoff` (HIGH, not declarable): repointing the boundaries, editing either file,
or removing the `boundaries` block, which before this change gave only a LOW note. Only the owner clears it, with
`approve`, which re-freezes the current inputs, or by restoring the files. Labels stay unsigned: the
presentation file's own hash is not frozen. A check that failed cannot be signed over (the sign-off refuses).

## 3. Alternatives considered

- **A check inside `schema.validate`.** This would run at every validation, including once per candidate
  engine and inside the analysis steps, and it would make the analysis refuse a signed spec when an unsigned
  boundary file changes. The check needs only the units table and the boundaries, so it runs once in `h_SPEC`
  and at the gate.
- **A new checks config for the boundaries.** The figures already declare them, so a second declaration
  could disagree with the first. The keys and file discovery are the figures' own. The declaration parser is
  duplicated for now, because PR #127 is not merged, and the second PR to land imports the other's (agreed
  with the figures work).
- **A geometry library (shapely/pyproj).** Not needed at this accuracy, and it would add a dependency to the
  harness image.
- **An edge tolerance relative to unit size.** A fixed distance matches the error sources (simplification,
  rounding), which do not scale with the unit. A reciprocal swap between small adjacent units is still found,
  because the swap test ignores distance.

## 4. Tests (written first)

`toolkit/spec/geometry.py --self-test`:
- IG1: area and distance against closed forms; a hole; a MultiPolygon.
- IG2: a synthetic GeoJSON of squares and a units table with one clean unit, one edge-case point just outside
  its square, one reciprocal swapped pair, one area mismatch, one non-reciprocal stray point whose area
  escalates, one unit with no crosswalk row, two units claiming one feature, a MultiPolygon unit, and a point
  in a hole. Also an edge-case point that does not escalate an area mismatch.
- IG3: pre-sign problems, the gate's view, and acceptance by name (accepted, stale and empty reasons).
- IG4: not run without boundaries or coordinates; the column-name fallback; no area column.
- IG5: fail closed on malformed GeoJSON (7 shapes), a malformed block, a missing declared column, a conflicting
  crosswalk, two presentation files, and missing thresholds.
- IG6: thresholds read from the config.
- IG7: no disease, place or intervention words in the module (the `pipeline.domain_terms` scan).

Added after the review of #131, written first:
- IG8: a HIGH that the spec proposes stays HIGH at the gate and waits for the owner. A MEDIUM proposal is
  accepted. A lead sign-off approves nothing. `approve` refuses anyone but the owner. The owner's sign-off
  approves the proposals.
- IG9: a label edit changes nothing. Repointing to polygons with swapped ids, removing `boundaries`, or editing
  the boundary file after sign-off is HIGH. An approval lapses with its evidence. `approve` re-freezes. An
  unreadable record fails closed.
- IG10: collinear, two-vertex and zero-area rings fail closed, as outer rings and as holes.
- IG11: nested polygons do not crash the pair test.
- IG12: closed forms (a 1-degree cell and an octant, to 1e-9), and densifying a rectangle edge or a sloped
  triangle edge leaves the area unchanged.

Through the machine (`agents/stage_machine.py`):
- IG-M1: a planted swap with no proposal refuses the spec before SIGN.
- IG-M2: a proposed swap reaches SIGN and stays HIGH until the owner signs; then the run goes on.
- IG-M3: the gate carries the open finding.
- IG-M4: under `--signoff lead` the run pauses for the owner instead of lead-signing.
- IG-M5: both records are harness files.

`toolkit/spec/signoff.py`: IG-S1, the sheet section, and IG-S2, the sign-off writes the geometry record.

## 5. Real check (local only; counts and field names, no data)

Run against the boundary file, crosswalk and geography table of the run in §1 (fields `lon`, `lat`,
`area_km2`; crosswalk keyed unit id to feature id) with
`python3 toolkit/spec/geometry.py table ...`. The results are in the PR description, as counts only.

## 6. Open items

- When PR #127 or this PR lands second, figures.py and this module should share one boundaries loader (the
  figures read exterior rings only; this check needs holes).
- With `--signoff none` there is no sign-off, so a proposed HIGH exception is never approved, and the gate
  cannot accept the run (fail closed).
- A run signed before this change has no geometry record, so nothing is frozen for it. The first owner
  `approve` creates the record.
- The check finds rows whose geometry fields disagree with the boundaries. It cannot tell which other columns
  (population, raster-derived values) came along with the swapped geometry. The finding says they probably
  did, and the fix belongs in the derivation.
