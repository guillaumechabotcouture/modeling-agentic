#!/usr/bin/env python3
"""The Figures view (docs/design/review-workbench.md §5.12): the figure set a report is built from, each figure with
its sources, whether they still have the hashes it was drawn from, whether the manifest lists its image, where a
report uses it, and the view it belongs beside.

    python3 tools/workbench/views/figures.py --self-test

It reads analysis/figures.yaml (two producers write it under one schema: toolkit/analysis/figures.py and
toolkit/analysis/estimates.py; the adapter is chosen by the `producer` field), each figure's image and provenance
record under figures/, analysis/manifest.json, the verified signed spec, and report.md, manuscript.md, results.md
and report_render.json. It reads only, through the run's confined reader.

The rules, with each place §5.12 is silent marked "assumed":

 A1  facts: n_figures, n_produced, n_not_produced, n_stale, n_missing_file, n_used, and n_<tier> and
     n_<tier>_produced for each tier the run records (its `tiers`, or its entries' own tier where it records none).
 A2  tables.figures has the fourteen columns of §5.12 and then `view` (settled). `sources` and `used_in` are joined
     with ", " in recorded order (used_in in the order report.md, manuscript.md, results.md, report_render.json), and
     used_in is "" for a figure no text names (assumed). A column the producer does not record is null. For a figure
     not produced, path, table, fresh and in_manifest are null, sources is "" and a text names it when it holds
     figures/<figure>.png or that file name (assumed).
 A3  `fresh` is the worst state of the figure's recorded sources: missing, changed, not_recorded, same, in that
     order (assumed order; the words are settled). A figure with no readable provenance record, or a record that
     holds no source, is not_recorded.
 A4  tables.sources (id, figure, source, recorded, now, state) has one row per recorded source hash and per
     `presentation` entry of each figure's provenance record; `now` for the spec is the hash of the verified signed
     copy. With no verified signed copy the spec's `now` is null and its state is `missing` when the signed copy
     cannot be read and `not_recorded` when the sign-off record does not hold its hash (assumed).
 A5  figures.figure_fresh: `problem` when a source changed or is missing; `not_available` when a figure's
     provenance record is missing or cannot be parsed, or holds no valid hash for a source, and nothing is stale;
     `ok` otherwise. A spec on disk that differs from the signed copy does not make a figure stale.
 A6  figures.figure_missing: `look` for a figure not produced; `problem` when a report names it, and when the index
     lists a figure as produced whose image is not in the run; `not_available` for an entry with no path (assumed).
 A7  figures.figure_unused: `look` for a produced decision-tier figure that no text names; `not_available` when the
     run has neither report.md nor manuscript.md, or one of the texts is there and cannot be read (assumed).
 A8  figures.figure_in_manifest: `problem` when a produced figure's image is not among the manifest's deliverables,
     or the manifest's hash for it is not the file's (a missing file is never a match).
 A9  `images` is a list beside checks, facts, tables, charts and notes; its one entry names tables.images, which
     starts with the five settled columns (figure, path, sha256, title, caption) and has one row per produced figure
     whose image could be read, with the hash of the image as read. Assumed, so that the page can show each figure
     under its tier with what the reader needs beside it: the table goes on with tier, shows, sources, fresh,
     used_in and view, and the entry names them (`group`: the column images are gathered under; `details`: the
     columns listed under each image).
 A10 An unknown producer or schema makes all four checks `not_available`, naming it. notes.producer names the
     producer and, for the estimates producer, says it records no title, caption or table (assumed name).
 A11 `view` (settled: tier to view) is read from the figure's tier and group where they decide it (the simulation
     tier beside scenarios; the decision tier or group beside outcomes), else from the figure's name by the list of
     §5.12; a figure neither places is null. tables.not_produced (figure, tier, reason, used_in, view) repeats the
     figures not produced, so the page can show them on their own (assumed).
"""
from __future__ import annotations

import os
import re
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
import checks  # noqa: E402

FACTS = ("n_figures", "n_produced", "n_stale", "n_not_produced", "n_used", "n_missing_file")     # and two per tier
COLUMNS = {
    "figures": ["figure", "tier", "group", "title", "caption", "shows", "produced", "reason", "path", "table", "sources",
                "fresh", "in_manifest", "used_in", "view"],
    "sources": ["id", "figure", "source", "recorded", "now", "state"],
    "images": ["figure", "path", "sha256", "title", "caption", "tier", "shows", "sources", "fresh", "used_in", "view"],
    "not_produced": ["figure", "tier", "reason", "used_in", "view"],
}
CHECKS = ("figure_fresh", "figure_missing", "figure_unused", "figure_in_manifest")
TITLES = {"figure_fresh": "Each figure's sources still have the hashes it was drawn from",
          "figure_missing": "Every figure of the set was produced, with its image",
          "figure_unused": "Every decision figure is named by a report",
          "figure_in_manifest": "Every figure's image is in the analysis manifest with its hash"}
IMAGES = {"table": "images", "path": "path", "hash": "sha256", "title": "title", "caption": "caption", "group": "tier",
          "details": ["shows", "sources", "fresh", "used_in", "view"]}
TEXTS = ("report.md", "manuscript.md", "results.md", "report_render.json")
REPORTS = TEXTS[:2]                                    # with neither, no figure can be said to be unused (§5.12)
INDEX, MANIFEST, SCHEMA = "analysis/figures.yaml", "analysis/manifest.json", "analysis.figures/1"
SPEC, SIGNED = checks.SPEC, "decision_brief.signed/decision_spec.yaml"
WORST = ("missing", "changed", "not_recorded", "same")     # A3
MAX_TEXT = 8 << 20                                     # a report text larger than this is not searched
SHOWN = 8                                              # names listed in a sentence; the check's rows hold them all
# §5.12, tier to view: the fallback by name for a figure whose tier and group do not decide it.
BESIDE = (("spec", ("data_coverage", "unit_attributes"), ()), ("model", ("parameters", "design_coverage", "sensitivity"), ()),
          ("fit", ("burden_validation",), ("calibration", "posterior", "diagnostics")), ("outcomes", (), ("estimates",)))


def _text(x):
    return x if isinstance(x, str) and x else None


def _standard(e):
    return {"figure": _text(e.get("figure")), **{k: _text(e.get(k)) for k in ("tier", "group", "title", "caption", "shows", "path", "table")},
            "sources": e.get("sources")}


def _estimates(e):      # toolkit/analysis/estimates.py _fig(): id, path, svg, sources, tier and nothing else
    return {"figure": _text(e.get("id")), "tier": _text(e.get("tier")), "group": None, "title": None, "caption": None, "shows": None,
            "path": _text(e.get("path")), "table": None, "sources": e.get("sources")}


ADAPTERS = {"toolkit/analysis/figures.py": (_standard, "the standard figure set"),
            "toolkit/analysis/estimates.py": (_estimates, "the figures of the estimates deliverable")}


def beside(name, tier, group):
    """The id of the view a figure belongs beside (§5.12, tier to view), or None."""
    if tier == "simulation":
        return "scenarios"
    if tier == "decision" or group == "decision":
        return "outcomes"
    for view_id, names, starts in BESIDE:
        if name in names or any(name.startswith(s) for s in starts):
            return view_id
    return "fit" if tier == "diagnostics" else None


def _none(why, evidence):
    """The result when the figure index cannot be read or recognised: nothing is counted and nothing is listed."""
    return {"checks": [checks.result(f"figures.{n}", "figures", TITLES[n], "not_available", why, evidence) for n in CHECKS],
            "facts": {k: None for k in FACTS}, "tables": {n: None for n in COLUMNS}, "charts": [], "notes": {"figures": why}, "images": []}


def _signed(run):
    """(the hash of the verified signed copy of the spec or None, the state of a spec source when there is none, a
    sentence). Verified: the signed copy has the hash the sign-off record holds for the spec (§3)."""
    sha = run.sha(SIGNED)
    so, so_rel, why = checks.signoff_record(run)
    if sha is None:
        return None, "missing", f"The run has no readable signed copy of the spec ({SIGNED}), so a figure's spec source has nothing to be compared with."
    if checks.bound(so, SPEC, sha) != "same":
        return None, "not_recorded", (f"The signed copy of the spec ({SIGNED}) is not verified: "
                                      + (f"{why}" if so is None else f"the sign-off record ({so_rel}) does not hold its hash")
                                      + ", so a figure's spec source cannot be compared.")
    differs = run.sha(SPEC) != sha
    return sha, None, (f"A figure's spec source is compared with the verified signed copy, {SIGNED} ({sha[:10]}; signed by "
                       f"{so.get('signed_by')}, {so_rel})." + (f" The spec on disk ({SPEC}) differs from it: that is the Steps view's "
                                                                 "finding, and makes no figure stale." if differs else ""))


def _record(run, name, path, signed, spec_state):
    """The rows of tables.sources for one figure from its provenance record, or (None, why) when the record cannot be
    read as {source_hashes: {file: hash}, presentation: [{path, sha256}]}."""
    rel = f"{path}.provenance.json"
    rec, why = run.json(rel)
    if rec is None:
        return None, why
    hashes, shown = rec.get("source_hashes"), rec.get("presentation", [])
    if not isinstance(hashes, dict) or not isinstance(shown, list) or not all(isinstance(p, dict) and _text(p.get("path")) for p in shown):
        return None, f"{rel} does not hold source_hashes as a mapping and presentation as a list of files"
    rows, seen = [], set()
    for src, recorded in list(hashes.items()) + [(p["path"], p.get("sha256")) for p in shown]:
        src = str(src)
        if src in seen:
            continue
        seen.add(src)
        if src == SPEC:      # §5.12: against the verified signed copy, never the file on disk
            now, state = signed, spec_state or checks.hash_state(recorded, signed)
        else:
            now = run.sha(src)
            state = checks.hash_state(recorded, now)
        rows.append([f"{name}|{src}", name, src, recorded if isinstance(recorded, str) else None, now, state])
    return rows, (None if rows else f"{rel} records no source")


def _listed(names, more=""):
    return ", ".join(names[:SHOWN]) + (f" and {len(names) - SHOWN} more" if len(names) > SHOWN else "") + more


def view(run):
    ev = [(INDEX, "schema, producer, data.figures, data.not_produced, data.tiers")]
    doc, why = run.large(INDEX)
    if doc is None:
        return _none(why, ev)
    schema, producer = doc.get("schema"), doc.get("producer")
    if schema != SCHEMA:
        return _none(f"{INDEX} has the schema {schema!r}, which this view does not read (it reads {SCHEMA}).", ev)
    if producer not in ADAPTERS:
        return _none(f"{INDEX} was written by {producer!r}, a producer this view has no adapter for (it has one for "
                     f"{' and '.join(ADAPTERS)}).", ev)
    adapt, set_name = ADAPTERS[producer]
    data = doc.get("data") if isinstance(doc.get("data"), dict) else {}
    figs, notp = data.get("figures"), data.get("not_produced") or []
    entries = [adapt(e) for e in figs] if isinstance(figs, list) and all(isinstance(e, dict) for e in figs) else None
    if entries is None or any(e["figure"] is None for e in entries):
        return _none(f"{INDEX} holds no list of figures, each with its name (data.figures).", ev)
    if not isinstance(notp, list) or not all(isinstance(e, dict) and _text(e.get("figure")) for e in notp):
        return _none(f"{INDEX} holds no list of the figures not produced, each with its name (data.not_produced).", ev)
    produced = {e["figure"]: e for e in reversed(entries)}
    absent = {e["figure"]: e for e in reversed(notp) if e["figure"] not in produced}
    tier_of, order = {}, []
    for tier, names in (data.get("tiers") if isinstance(data.get("tiers"), dict) else {}).items():
        for n in names if isinstance(names, list) else []:
            if isinstance(n, str) and n not in tier_of:
                tier_of[n] = str(tier)
                order.append(n)
    order += [e["figure"] for e in entries + notp if e["figure"] not in tier_of and e["figure"] not in order]

    texts, unread = {}, []
    for rel in TEXTS:
        b, why = run.read(rel, MAX_TEXT + 1)
        if b is not None and len(b) <= MAX_TEXT:
            texts[rel] = b.decode("utf-8", "replace")
        elif b is not None or not str(why).endswith("is not in the run folder"):
            unread.append(f"{rel} is larger than {MAX_TEXT >> 20} MB and is not searched" if b is not None else why)
    man, man_why = run.json(MANIFEST)
    listed = man.get("deliverables") if isinstance(man, dict) else None
    if man is not None and not isinstance(listed, dict):
        listed, man_why = None, f"{MANIFEST} holds no mapping of deliverables"
    signed, spec_state, spec_says = _signed(run)

    rows, sources, images, stale, unrecorded, no_path, no_image, outside = [], [], [], [], [], [], [], []
    for name in order:
        e, m = produced.get(name), absent.get(name, {})
        tier = (e or {}).get("tier") or _text(m.get("tier")) or tier_of.get(name)
        group, path = (e or {}).get("group"), (e or {}).get("path")
        file_name = re.escape(os.path.basename(path or f"figures/{name}.png"))
        used = ", ".join(rel for rel, text in texts.items() if re.search(rf"(?<![\w.-]){file_name}(?!\w|\.\w)", text))
        where = beside(name, tier, group)
        fresh = in_manifest = image = None
        srcs = ", ".join(str(s) for s in e["sources"]) if e and isinstance(e.get("sources"), list) else ""
        if e and path is None:
            no_path.append(name)
        elif e:
            image = run.sha(path)
            if image is None:
                no_image.append([name, path, run.read(path, 1)[1]])
            got, why = _record(run, name, path, signed, spec_state)
            sources += got or []
            states = [r[5] for r in got or []]
            fresh = next((w for w in WORST if w in states), "not_recorded")
            if fresh in ("missing", "changed"):
                stale.append([name, ", ".join(f"{r[2]} {r[5]}" for r in got if r[5] in ("missing", "changed"))])
            elif fresh == "not_recorded":
                unrecorded.append([name, why or "no valid hash is recorded for " + ", ".join(r[2] for r in got if r[5] == "not_recorded")])
            if listed is not None:
                rec = listed.get(path)
                in_manifest = isinstance(rec, dict) and checks.hash_state(rec.get("sha256"), image) == "same"
                if not in_manifest:
                    outside.append([name, path, "not listed" if rec is None else "listed with another hash, or with none, or the image is missing"])
            if image is not None:
                images.append([name, path, image, e["title"], e["caption"], tier, e["shows"], srcs, fresh, used, where])
        rows.append([name, tier, group, (e or {}).get("title"), (e or {}).get("caption"), (e or {}).get("shows"), e is not None,
                     None if e else _text(m.get("reason")), path, (e or {}).get("table"), srcs, fresh, in_manifest, used, where])

    col = {c: i for i, c in enumerate(COLUMNS["figures"])}
    made, not_made = [r for r in rows if r[col["produced"]]], [r for r in rows if not r[col["produced"]]]
    tiers = list(dict.fromkeys(list(dict.fromkeys(tier_of.values())) + [r[col["tier"]] for r in rows if r[col["tier"]]]))
    facts = {"n_figures": len(rows), "n_produced": len(made), "n_stale": len(stale), "n_not_produced": len(not_made),
             "n_used": sum(1 for r in rows if r[col["used_in"]]), "n_missing_file": len(no_image)}
    clash = [t for t in tiers if f"n_{t}" in facts or f"n_{t}_produced" in facts]
    for t in tiers:
        if t not in clash:
            facts[f"n_{t}"] = sum(1 for r in rows if r[col["tier"]] == t)
            facts[f"n_{t}_produced"] = sum(1 for r in made if r[col["tier"]] == t)

    def result(name, status, says, evidence, table=None):
        return checks.result(f"figures.{name}", "figures", TITLES[name], status, says, evidence,
                             {"columns": table[0], "rows": table[1]} if table and table[1] else None)

    # ---- figure_fresh
    rec_ev = [("figures/<name>.png.provenance.json", "source_hashes, presentation"), (SIGNED, "the file's hash"), (INDEX, "data.figures[].path")]
    detail = (["figure", "what"], stale + unrecorded + [[n, "the index records no path, so its record cannot be found"] for n in no_path])
    if stale:
        says = (f"{len(stale)} of {len(made)} produced figures have a source that changed or is missing since they were drawn: "
                + _listed([f"{n} ({what})" for n, what in stale]) + ". A stale figure does not show the files now in the run.")
    elif unrecorded or no_path:
        says = (f"The freshness of {len(unrecorded) + len(no_path)} of {len(made)} produced figures cannot be judged: "
                + _listed([f"{n} ({what})" for n, what in detail[1]]) + ".")
    elif not made:
        says = "No figure was produced, so there is nothing to compare."
    else:
        says = (f"All {len(made)} produced figures: every recorded source has the hash it was drawn from (the spec against the "
                f"signed copy {SIGNED}).")
    if stale and (unrecorded or no_path):
        says += f" The freshness of {len(unrecorded) + len(no_path)} more cannot be judged ({_listed([u[0] for u in unrecorded] + no_path)})."
    fresh_check = result("figure_fresh", "problem" if stale else "not_available" if unrecorded or no_path or not made else "ok", says, rec_ev, detail)

    # ---- figure_missing
    relied = [r for r in not_made if r[col["used_in"]]]
    parts, status = [], "ok"
    if not_made:
        status = "look"
        parts.append(f"{len(not_made)} of {len(rows)} figures not produced: "
                     + _listed([f"{r[0]}: {r[col['reason']] or 'the index gives no reason'}" for r in not_made], "."))
    if no_path:
        status = "not_available"
        parts.append(f"The index records no path for {_listed(no_path)}, so whether its image is in the run cannot be said.")
    if relied or no_image:
        status = "problem"
    if relied:
        parts.append("A report relies on a figure that was not produced: " + _listed([f"{r[0]} (named in {r[col['used_in']]})" for r in relied]) + ".")
    if no_image:
        parts.append(f"The index lists {len(no_image)} figure(s) as produced whose image cannot be read: "
                     + _listed([f"{n} ({why})" for n, _, why in no_image]) + ".")
    if unread:
        parts.append(f"Not searched for figure names: {'; '.join(unread)}.")
    missing_check = result("figure_missing", status, " ".join(parts) if status != "ok" else
                           f"All {len(rows)} figures of the set are produced, each with its image." + (f" {parts[-1]}" if unread else ""),
                           [(INDEX, "data.not_produced[].reason, data.figures[].path"), ("figures/*.png", "the file"), (", ".join(TEXTS), "figure paths and file names")],
                           (["figure", "tier", "produced", "what"], [[r[0], r[col["tier"]], False, r[col["reason"]]] for r in not_made]
                            + [[n, None, True, why] for n, _, why in no_image] + [[n, None, True, "no path recorded"] for n in no_path]))

    # ---- figure_unused
    unused = [r[0] for r in made if r[col["tier"]] == "decision" and not r[col["used_in"]]]
    text_ev = [(", ".join(TEXTS), "figure paths and file names"), (INDEX, "data.figures[].tier, path")]
    if unread or not any(rel in texts for rel in REPORTS):
        unused_check = result("figure_unused", "not_available", (f"A text cannot be searched ({'; '.join(unread)}), so no figure can be said to be unused." if unread else
                              f"The run has neither {REPORTS[0]} nor {REPORTS[1]}, so no figure can be said to be unused."), text_ev)
    elif unused:
        unused_check = result("figure_unused", "look", f"{len(unused)} produced decision-tier figure(s) are named by path or file name in no text "
                              f"({', '.join(texts)}): {_listed(unused)}.", text_ev, (["figure"], [[n] for n in unused]))
    else:
        unused_check = result("figure_unused", "ok", "Every produced decision-tier figure is named by its path or file name in a text "
                              f"({', '.join(texts)})." if any(r[col["tier"]] == "decision" for r in made) else
                              "No decision-tier figure was produced, so none is unused.", text_ev)

    # ---- figure_in_manifest
    man_ev = [(MANIFEST, "deliverables[path].sha256"), ("figures/*.png", "the file's hash")]
    if listed is None:
        manifest_check = result("figure_in_manifest", "not_available", man_why, man_ev)
    elif outside:
        manifest_check = result("figure_in_manifest", "problem", f"{len(outside)} of {len(made)} produced images are not in the manifest with the "
                                f"hash of the file: {_listed([f'{n} ({what})' for n, _, what in outside])}. An image the manifest does not hold "
                                "with its hash cannot be said to be the one the analysis wrote.", man_ev, (["figure", "path", "what"], outside))
    elif no_path or not made:
        manifest_check = result("figure_in_manifest", "not_available", "No figure was produced, so there is nothing to look for." if not made else
                                f"The index records no path for {_listed(no_path)}, so its image cannot be looked for in the manifest.", man_ev)
    else:
        manifest_check = result("figure_in_manifest", "ok", f"All {len(made)} produced images are listed in the manifest with the hash of the file.", man_ev)

    recorded_no = "" if adapt is _standard else (" This producer records no title, caption, what-it-shows, table or group for a figure, so "
                                                 "those columns are empty.")
    notes = {"producer": f"{INDEX} was written by {producer}: {set_name}.{recorded_no} Tier names are the run's own.",
             "spec": spec_says,
             "used": "A figure is used when a text (" + ", ".join(TEXTS) + ") holds its path or its file name; a bare figure name is an "
                     "ordinary word and does not count. Texts in this run: " + (", ".join(texts) or "none") + ".",
             "view": "The view column names the view each figure belongs beside: spec (data coverage and unit attributes), model "
                     "(parameters, design coverage, sensitivity), fit (calibration figures, validation and diagnostics), scenarios "
                     "(the simulation tier), outcomes (the decision tier and estimate figures)."}
    if clash:
        notes["tiers"] = f"No count is given for the tier(s) {', '.join(clash)}: the name would be read as another fact."
    over = data.get("overviews")
    if isinstance(over, dict) and over:
        notes["overviews"] = "The harness also drew one overview image per tier: " + ", ".join(f"{k} ({v})" for k, v in over.items()) + "."
    i_tier, i_view = col["tier"], col["view"]
    tables = {
        "not_produced": {"columns": COLUMNS["not_produced"], "wrap": ["reason"],
                         "rows": [[r[0], r[i_tier], r[col["reason"]], r[col["used_in"]], r[i_view]] for r in not_made],
                         "about": f"The figures of the set that were not produced, each with the harness's reason ({INDEX} data.not_produced). "
                                  "used_in names a text that relies on one all the same."},
        "sources": {"columns": COLUMNS["sources"], "rows": sources,
                    "about": "One row per file a figure was drawn from (its provenance record's source_hashes, then its presentation "
                             "entries): the hash recorded when the figure was drawn, the hash now, and the state. For the spec, now is "
                             "the verified signed copy's hash."},
        "figures": {"columns": COLUMNS["figures"], "rows": rows, "wrap": ["caption"],
                    "about": f"One row per figure of the set, by tier in the run's order ({INDEX}). fresh is the worst state of its "
                             "sources; in_manifest says whether the manifest holds its image with the file's hash; used_in names the "
                             "texts that hold its path or file name; view is the view it belongs beside."},
        "images": {"columns": COLUMNS["images"], "rows": images,
                   "about": "One row per produced figure whose image could be read, with the hash of the image as read: the page asks "
                            "the server for exactly these bytes."}}
    return {"checks": [fresh_check, missing_check, unused_check, manifest_check], "facts": facts, "tables": tables, "charts": [],
            "notes": notes, "images": [dict(IMAGES)]}


# ---------------------------------------------------------------------------------------------- self-test

def _self_test():
    import hashlib
    import json

    import workbench
    from views.fit_bench import Bench, check, fixture_builder, need, rewrite, says, statuses
    fx = fixture_builder()
    b = Bench(lambda run: view(run))        # the module's `view` as it is when a case runs
    folder, look_at, case, edited = b.folder, b.look_at, b.case, b.edited
    hm, post = "history_matching", "posterior"
    index_file, manifest_file, labels_file = fx.FIGS, "analysis/manifest.json", fx.LABELS
    spec_file, signed_file, signoff_file = "decision_spec.yaml", "decision_brief.signed/decision_spec.yaml", "decision_spec.signoff.json"
    builds = (("standard, history matching", hm, "standard"), ("standard, posterior", post, "standard"), ("estimates", post, "estimates"))
    tiers = {"standard": ("data_model", "simulation", "decision"), "estimates": ("diagnostics", "decision")}

    # ---- readers of a view result
    def table(v, name):
        t = v["tables"].get(name)
        need(isinstance(t, dict) and t.get("columns") == COLUMNS[name] and isinstance(t.get("rows"), list),
             f"tables.{name} is {{columns, rows, about}} with columns {COLUMNS[name]} (got {t.get('columns') if isinstance(t, dict) else t})")
        need(all(isinstance(r, list) and len(r) == len(COLUMNS[name]) for r in t["rows"]), f"tables.{name} rows are lists in column order")
        return [dict(zip(COLUMNS[name], r)) for r in t["rows"]]

    def row(v, name, key, value):
        found = [r for r in table(v, name) if r[key] == value]
        need(len(found) == 1, f"one row of tables.{name} with {key} {value!r} (found {len(found)})")
        return found[0]

    def holds(got, want, what):
        off = {k: (got.get(k), w) for k, w in want.items() if got.get(k) != w or type(got.get(k)) is not type(w)}
        need(not off, f"{what}: {off} (got, wanted)")

    def word(state):
        return state       # §5.12, settled: `fresh` uses the words same, changed, missing and not_recorded

    def sha(d, rel):
        with open(os.path.join(d, rel), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    # ---- edits of a copy of a built run
    def both(*edits):
        return lambda d: [e(d) for e in edits]

    def rejson(rel, change):
        def edit(d):
            with open(os.path.join(d, rel), encoding="utf-8") as f:
                doc = json.load(f)
            change(doc)
            with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=1)
        return edit

    def retext(rel, change):
        def edit(d):
            with open(os.path.join(d, rel), encoding="utf-8") as f:
                text = f.read()
            with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
                f.write(change(text))
        return edit

    def remove(*rels):
        return lambda d: [os.remove(os.path.join(d, rel)) for rel in rels]

    def record(name, change):
        return rejson(f"figures/{name}.png.provenance.json", change)

    unverified = {"the sign-off record is missing": remove(signoff_file),
                  "the sign-off record holds another hash for the spec": rejson(signoff_file, lambda so: so["sha256"].update({spec_file: "0" * 64})),
                  "the signed copy is missing": remove(signed_file)}

    # ------------------------------------------------------------------------------- the generic model (§5.12)
    def g_shape():
        for label, method, figures in builds:
            v = look_at(folder(method, figures=figures))
            want = sorted(FACTS + tuple(f"n_{t}{end}" for t in tiers[figures] for end in ("", "_produced")))
            need(sorted(v["facts"]) == want, f"{label}: facts are exactly {want} (got {sorted(v['facts'])})")
            need([c.get("id") for c in v["checks"]] == [f"figures.{n}" for n in CHECKS], f"{label}: the four checks, in this order (got {[c.get('id') for c in v['checks']]})")
            need(all(c.get("step") == "figures" and str(c.get("says") or "").strip() and c.get("evidence") for c in v["checks"]),
                 f"{label}: each check names its view, one sentence and the file and field it read")
            need(statuses(v) == {n: "ok" for n in CHECKS}, f"{label}: on the clean run every check is ok (got {statuses(v)})")
            need(set(v["tables"]) >= set(COLUMNS) and all(isinstance(t.get("about"), str) and t["about"].strip() for t in v["tables"].values() if t),
                 f"{label}: tables figures, sources and images, each with what its rows are (got {sorted(v['tables'])})")
            need(workbench.chart_problems(v) == [], f"{label}: any chart is one the page can draw ({workbench.chart_problems(v)})")

    def g_facts():
        holds(look_at(folder(hm))["facts"], {
            "n_figures": 13, "n_produced": 13, "n_not_produced": 0, "n_data_model": 8, "n_data_model_produced": 8, "n_simulation": 2,
            "n_simulation_produced": 2, "n_decision": 3, "n_decision_produced": 3, "n_stale": 0, "n_missing_file": 0, "n_used": 4},
            "the clean run, standard set")
        holds(look_at(folder(post, figures="estimates"))["facts"], {
            "n_figures": 4, "n_produced": 4, "n_not_produced": 0, "n_diagnostics": 2, "n_diagnostics_produced": 2, "n_decision": 2,
            "n_decision_produced": 2, "n_stale": 0, "n_missing_file": 0, "n_used": 3}, "the clean run, estimates set")
        holds(look_at(folder(hm, "figure_not_produced"))["facts"], {
            "n_figures": 13, "n_produced": 12, "n_not_produced": 1, "n_simulation": 2, "n_simulation_produced": 1}, "a figure not produced")
        holds(look_at(folder(hm, "figure_stale_source"))["facts"], {"n_stale": 1, "n_missing_file": 0}, "a stale figure")
        holds(look_at(folder(hm, "figure_image_missing"))["facts"], {"n_produced": 13, "n_missing_file": 1, "n_stale": 0}, "a produced figure whose image is absent")
        holds(look_at(folder(hm, "figure_unused"))["facts"], {"n_used": 3}, "a figure no text names")

    def g_rows():
        d = folder(hm)
        v = look_at(d)
        got = table(v, "figures")
        need([r["figure"] for r in got][:2] == ["data_coverage", "parameters"] and [r["figure"] for r in got][-5:] ==
             ["timeline", "trajectories", "headline", "frontier", "decision_tests"], "one row per figure of the set, by tier in the run's order")
        holds(row(v, "figures", "figure", "headline"), {
            "tier": "decision", "group": "decision", "title": "the plan against the comparator, with its interval",
            "caption": "The plan against the comparator, with its interval; drawn by the harness from the files listed as its sources.",
            "shows": "headline result", "produced": True, "reason": None, "path": "figures/headline.png", "table": "figures/headline.csv",
            "sources": "analysis/point.yaml, analysis/uncertainty.yaml, decision_spec.yaml, data/derived/units.csv", "in_manifest": True,
            "used_in": "report.md"}, "a decision figure")
        need(all(word(r["fresh"]) == "same" for r in got), "every figure of the clean run is fresh")
        holds(row(v, "figures", "figure", "decision_tests"), {"used_in": "report.md"}, "a figure a text names by its file name alone is used")
        holds(row(v, "figures", "figure", "data_coverage"), {"tier": "data_model", "group": "diagnostics", "used_in": "manuscript.md"}, "a figure the manuscript names")
        holds(row(v, "figures", "figure", "trajectories"), {"used_in": ""}, "a bare figure name in a sentence is an ordinary word and names no figure")
        v = look_at(folder(hm, "figure_not_produced"))
        holds(row(v, "figures", "figure", "trajectories"), {
            "tier": "simulation", "produced": False, "reason": fx.NO_TRAJECTORIES, "path": None, "table": None, "sources": "", "fresh": None,
            "in_manifest": None, "used_in": ""}, "a figure not produced, with the harness's reason (assumed: A2)")
        holds(row(look_at(folder(hm, "figure_not_produced_named")), "figures", "figure", "trajectories"), {"produced": False, "used_in": "report.md"},
              "a figure not produced that a report names by path")

    def g_estimates():
        v = look_at(folder(post, figures="estimates"))
        got = table(v, "figures")
        need([r["figure"] for r in got] == ["diagnostics_ppc", "diagnostics_rt", "estimates_q1", "estimates_q2"], "the entry's id is the figure's name")
        holds(got[0], {"tier": "diagnostics", "group": None, "title": None, "caption": None, "shows": None, "table": None, "produced": True,
                       "path": "figures/diagnostics_ppc.png", "sources": "analysis/estimates.yaml", "in_manifest": True, "used_in": "manuscript.md"},
              "an entry of the estimates producer: what it does not record is null; its tier is shown under its own name")
        holds(got[3], {"tier": "decision", "used_in": "report.md"}, "a decision figure of the estimates producer")
        need("toolkit/analysis/estimates.py" in v["notes"].get("producer", "") and "caption" in v["notes"]["producer"],
             "notes.producer names the producer and says what it does not record (assumed: A10)")
        need("toolkit/analysis/figures.py" in look_at(folder(hm))["notes"].get("producer", ""), "and so for the standard producer")

    def g_sources():
        d = folder(hm)
        v = look_at(d)
        got = [r for r in table(v, "sources") if r["figure"] == "headline"]
        need([r["source"] for r in got] == ["analysis/point.yaml", "analysis/uncertainty.yaml", "data/derived/units.csv", "decision_spec.yaml", labels_file],
             f"one row per recorded source in the record's order, then per presentation entry (got {[r['source'] for r in got]})")
        need(all(r["id"] == f"headline|{r['source']}" for r in got), "the first column addresses the row: <figure>|<source>")
        now = sha(d, "analysis/point.yaml")
        holds(got[0], {"recorded": now, "now": now, "state": "same"}, "a source that has the hash it was drawn from")
        holds(got[4], {"recorded": sha(d, labels_file), "state": "same"}, "a presentation file is a source too")
        v = look_at(folder(hm, "figure_stale_source"))
        stale = row(v, "sources", "id", "headline|analysis/point.yaml")
        need(stale["state"] == "changed" and stale["recorded"] != stale["now"] == sha(folder(hm, "figure_stale_source"), "analysis/point.yaml"),
             "a changed source shows both hashes")
        need(word(row(v, "figures", "figure", "headline")["fresh"]) == "changed", "and its figure is no longer fresh")

    def g_images():
        for label, method, figures in builds:
            d = folder(method, figures=figures)
            v = look_at(d)
            need(v.get("images") == [IMAGES] and COLUMNS["images"][:5] == ["figure", "path", "sha256", "title", "caption"]
                 and set(IMAGES) - {"group", "details"} == {"table", "path", "hash", "title", "caption"}
                 and all(c in COLUMNS["images"] for c in [IMAGES["group"]] + IMAGES["details"]),
                 f"{label}: images is one declaration over tables.images, whose first five columns are the settled ones (got {v.get('images')!r})")
            got, shown = table(v, "images"), [r for r in table(v, "figures") if r["produced"]]
            need([r["path"] for r in got] == [r["path"] for r in shown], f"{label}: one row per produced figure")
            need(all(r["sha256"] == sha(d, r["path"]) for r in got), f"{label}: each with the hash of its image as read")
            need(all(all(r[k] == s[k] for k in COLUMNS["images"][3:]) for r, s in zip(got, shown)),
                 f"{label}: and the figure's own title, caption, tier, sources, freshness, use and view")
        v = look_at(folder(hm, "figure_image_missing"))
        need("figures/parameters.png" not in [r["path"] for r in table(v, "images")], "an image that is not in the run is not declared")

    def g_beside():
        v = look_at(folder(hm))
        got = {r["figure"]: r["view"] for r in table(v, "figures")}
        want = {"data_coverage": "spec", "parameters": "model", "calibration_fit": "fit", "timeline": "scenarios", "trajectories": "scenarios",
                "headline": "outcomes", "frontier": "outcomes", "decision_tests": "outcomes"}
        need({k: got.get(k) for k in want} == want, f"each figure names the view it belongs beside ({got})")
        need(all(n.startswith("calibration") for n, w in got.items() if w == "fit") and set(got.values()) <= {i for i, _, _ in workbench.PATH},
             f"every name is a view of the rail ({got})")
        got = {r["figure"]: r["view"] for r in table(look_at(folder(post, figures="estimates")), "figures")}
        need(got == {"diagnostics_ppc": "fit", "diagnostics_rt": "fit", "estimates_q1": "outcomes", "estimates_q2": "outcomes"},
             f"the estimates producer's figures: diagnostics beside the fit, estimates beside the outcomes ({got})")
        need(beside("something_else", "data_model", "diagnostics") is None and beside("anything", "simulation", None) == "scenarios",
             "a figure neither its tier nor its name places is beside no view, never a guess")

    def g_not_produced():
        need(table(look_at(folder(hm)), "not_produced") == [], "the clean run: no figure is missing from the set")
        v = look_at(folder(hm, "figure_not_produced_named"))
        holds(row(v, "not_produced", "figure", "trajectories"), {"tier": "simulation", "reason": fx.NO_TRAJECTORIES, "used_in": "report.md", "view": "scenarios"},
              "a figure not produced, with the harness's reason and the text that names it")
        tables = list(v["tables"])
        need(tables.index("not_produced") < tables.index("sources") < tables.index("figures"), f"the tables in the reader's order ({tables})")
        need(list(v["facts"])[:5] == ["n_figures", "n_produced", "n_stale", "n_not_produced", "n_used"], f"the counts in the reader's order ({list(v['facts'])})")

    # ------------------------------------------------------------------------------- the settled rules (§5.12)
    def t_signed_spec():
        d = folder(hm, "figure_spec_redrafted")
        need(sha(d, spec_file) != sha(d, signed_file), "the fixture's spec on disk differs from the signed copy")
        v = look_at(d)
        spec_rows = [r for r in table(v, "sources") if r["source"] == spec_file]
        need(len(spec_rows) == 13 and all(r["state"] == "same" and r["now"] == r["recorded"] == sha(d, signed_file) for r in spec_rows),
             "the spec as a source is compared with the verified signed copy, not with the file on disk")
        need(v["facts"]["n_stale"] == 0 and signed_file in " ".join(v["notes"].values()), "no figure is stale, and the view says which copy it read")
        for name, edit in unverified.items():
            v = look_at(edited(hm, edit))
            need(statuses(v)["figure_fresh"] not in ("ok", "look"), f"{name}: with no verified signed copy the spec source is not known to be the same")

    def t_stale():
        v = look_at(edited(hm, retext(labels_file, lambda text: text + "note: edited\n")))
        need(statuses(v)["figure_fresh"] == "problem" and v["facts"]["n_stale"] == 13, "a changed presentation file makes every figure that used it stale")
        v = look_at(edited(hm, remove(fx.UNC)))
        holds(row(v, "sources", "id", "headline|analysis/uncertainty.yaml"), {"now": None, "state": "missing"}, "a source that is no longer in the run")
        need(word(row(v, "figures", "figure", "headline")["fresh"]) == "missing" and v["facts"]["n_stale"] == 1 and statuses(v)["figure_fresh"] == "problem",
             "n_stale counts a figure with a missing source")
        c = check(look_at(folder(hm, "figure_stale_source")), "figure_fresh")
        need("headline" in says(c) and "analysis/point.yaml" in c["says"], f"the sentence names the figure and the source ({says(c)})")

    def t_used():
        base = "figure_unused"
        v = look_at(edited(hm, retext("manuscript.md", lambda text: text + "\nThe frontier is discussed below.\n"), defect=base))
        need(statuses(v)["figure_unused"] == "look" and row(v, "figures", "figure", "frontier")["used_in"] == "", "a bare figure name does not count")
        v = look_at(edited(hm, retext("results.md", lambda text: text + "\nSee frontier.png.\n"), defect=base))
        need(statuses(v)["figure_unused"] == "ok" and row(v, "figures", "figure", "frontier")["used_in"] == "results.md", "the file name alone, in the results text, counts")
        v = look_at(edited(hm, rejson("report_render.json", lambda doc: doc.update(figures=["figures/frontier.png"])), defect=base))
        need(statuses(v)["figure_unused"] == "ok" and row(v, "figures", "figure", "frontier")["used_in"] == "report_render.json", "so does the render record")
        v = look_at(edited(hm, retext("manuscript.md", lambda text: text.replace("figures/data_coverage.png", "figures/data_coverage.csv"))))
        need(row(v, "figures", "figure", "data_coverage")["used_in"] == "" and statuses(v)["figure_unused"] == "ok",
             "a figure outside the decision tier that no text names raises nothing")
        c = check(look_at(folder(hm, base)), "figure_unused")
        need("frontier" in says(c), f"the sentence names the unused figure ({says(c)})")

    def t_missing():
        c = check(look_at(folder(hm, "figure_not_produced")), "figure_missing")
        need("trajectories" in says(c) and "trajectory runs" in says(c), f"a figure not produced is listed with the harness's reason ({says(c)})")
        c = check(look_at(folder(hm, "figure_not_produced_named")), "figure_missing")
        need("report.md" in c["says"], f"and the sentence names the text that relies on it ({says(c)})")

    def planted(method, defect, figures):
        def fn():
            spec = fx.DEFECTS[defect]
            need(spec.get("view") == "figures" and spec["check"] in CHECKS, "the fixture records this edit for a Figures check")
            clean, v = statuses(look_at(folder(method, figures=figures))), look_at(folder(method, defect, figures=figures))
            want = {**clean, spec["check"]: spec["status"], **spec.get("also", {})}
            need(statuses(v) == want, f"only {spec['check']} (and {spec.get('also', {})}) moves: {({k: s for k, s in statuses(v).items() if want.get(k) != s})}")
            need(all(check(v, n)["says"].strip() for n in CHECKS), "each check says why")
        return fn

    # ------------------------------------------------------------------------------- fail closed (§3, §9)
    def y_none():
        for what, d in (("a run stopped before the analysis ended", folder(hm, upto="calibration")), ("no figure index", edited(hm, remove(index_file)))):
            v = look_at(d)
            need(statuses(v) == {n: "not_available" for n in CHECKS}, f"{what}: every check is not_available (got {statuses(v)})")
            need(all(v["tables"][n] is None for n in COLUMNS) and v["facts"]["n_figures"] is None and v.get("images") in ([], [IMAGES]),
                 f"{what}: tables and counts are null, never empty or zero")
            need("figures.yaml" in check(v, "figure_fresh")["says"], f"{what}: the sentence names the file")

    def y_unrecognised():
        for what, edit, name in (("a figure index that cannot be parsed", retext(index_file, lambda text: text[:len(text) // 2] + "\n  - [unclosed\n"), "figures.yaml"),
                                 ("another schema", rewrite(index_file, lambda doc: doc.update(schema="analysis.figures/2")), "analysis.figures/2"),
                                 ("an index whose figures is no list", rewrite(index_file, lambda doc: doc["data"].update(figures="none")), "figures")):
            v = look_at(edited(hm, edit))
            need(statuses(v) == {n: "not_available" for n in CHECKS}, f"{what}: every check is not_available (got {statuses(v)})")
            need(name in " ".join(c["says"] for c in v["checks"]), f"{what}: named in a sentence")
        c = check(look_at(folder(hm, "figures_unknown_producer")), "figure_fresh")
        need("toolkit/analysis/other.py" in c["says"], "an unknown producer is named, never guessed at")

    def y_reports():
        v = look_at(folder(hm, upto="analysis"))
        need(statuses(v) == {"figure_fresh": "ok", "figure_missing": "ok", "figure_unused": "not_available", "figure_in_manifest": "ok"},
             f"a run with no report and no manuscript yet: unused cannot be judged, the rest can (assumed: A7) (got {statuses(v)})")

    def y_manifest():
        v = look_at(edited(hm, remove(manifest_file)))
        need(statuses(v)["figure_in_manifest"] == "not_available" and all(r["in_manifest"] is None for r in table(v, "figures")),
             "no manifest: nothing is said to be in it or outside it")
        v = look_at(edited(hm, rejson(manifest_file, lambda doc: doc["deliverables"]["figures/headline.png"].update(sha256="abc"))))
        need(statuses(v)["figure_in_manifest"] == "problem" and row(v, "figures", "figure", "headline")["in_manifest"] is False,
             "a manifest hash that is no hash is never a match")

    def y_records():
        v = look_at(edited(hm, record("headline", lambda doc: doc["source_hashes"].update({"analysis/point.yaml": "abc"}))))
        need(word(row(v, "sources", "id", "headline|analysis/point.yaml")["state"]) == "not_recorded" and word(row(v, "figures", "figure", "headline")["fresh"]) == "not_recorded"
             and statuses(v)["figure_fresh"] == "not_available", "a recorded hash that is no hash is never a match, and not a stale source either")
        v = look_at(edited(hm, record("headline", lambda doc: doc.update(source_hashes=["analysis/point.yaml"]))))
        need(statuses(v)["figure_fresh"] == "not_available", "a record whose source hashes are no mapping is not read as no sources")
        v = look_at(edited(hm, record("frontier", lambda doc: doc["source_hashes"].update({"analysis/point.yaml": "0" * 64})), defect="figure_record_missing"))
        need(statuses(v)["figure_fresh"] == "problem", "a stale figure is a problem whatever else cannot be read")
        v = look_at(folder(hm, "figure_record_missing"))
        need(word(row(v, "figures", "figure", "calibration_fit")["fresh"]) == "not_recorded" and "calibration fit" in says(check(v, "figure_fresh")),
             "a figure with no provenance record is named, and its freshness is not recorded")

    def y_reads_only():
        d = folder(hm, "figure_stale_source")
        before = {os.path.join(at, f): sha(at, f) for at, _, files in os.walk(d) for f in files}
        look_at(d)
        need(before == {os.path.join(at, f): sha(at, f) for at, _, files in os.walk(d) for f in files}, "the view only reads")

    def w_source():
        with open(os.path.abspath(__file__), encoding="utf-8") as f:
            text = f.read().lower()
        need(not [w for w in fx.domain_words() if w in text], "no domain word in this file (§3a)")

    def w_words():
        for _, method, figures in builds:
            text = json.dumps(look_at(folder(method, figures=figures)), default=str).lower()
            need(not [w for w in fx.domain_words() if w in text], "no domain word in the view's result")

    case("G1 the generic model: six facts and two per tier of the run, four checks with ids figures.<name>, three tables; all ok on the clean run", g_shape)
    case("G2 facts hold the run's counts for both producers, and follow a planted edit", g_facts)
    case("G3 figures: the fourteen columns and `view`; used_in by path or file name only; a figure not produced with its reason (assumed: A2)", g_rows)
    case("G4 the estimates producer: id as the name, null title, caption, shows, table and group, its own tier names", g_estimates)
    case("G5 sources: id <figure>|<source>, recorded and present hash, state; presentation entries are sources too (assumed: A4)", g_sources)
    case("G6 images: one declaration over a table of path and hash, one row per image that can be read (assumed: A9)", g_images)
    case("G7 view: each figure names the view it belongs beside, by tier and group, else by name (§5.12, tier to view)", g_beside)
    case("G8 the figures not produced have their own table; counts and tables are in the reader's order (assumed: A11)", g_not_produced)
    case("T1 the spec as a source is compared with the verified signed copy; a redraft on disk makes no figure stale", t_signed_spec)
    case("T2 n_stale counts figures with a changed or missing source; the sentence names figure and source", t_stale)
    case("T3 used: the path or the file name in report, manuscript, results or the render record; never a bare name", t_used)
    case("T4 a figure not produced is listed with the harness's reason, and with the text that names it", t_missing)
    n = 0
    for method, figures, defects in ((hm, "standard", fx.FIGURES_DEFECTS),
                                     (post, "estimates", tuple(d for d in fx.FIGURES_DEFECTS if "estimates" in fx.figure_sets(d))),
                                     (post, "standard", ("figure_stale_source", "figure_not_produced_named", "figure_unused"))):
        for defect in defects:
            n += 1
            spec = fx.DEFECTS[defect]
            also = "".join(f", {k} {s}" for k, s in spec.get("also", {}).items())
            case(f"P{n} {method}, {figures} set, {defect}: {spec['check']} is {spec['status']}{also}, and no other check changes", planted(method, defect, figures))
    case("Y1 no figure index: every check not_available with the file's name; tables and counts null", y_none)
    case("Y2 an index that cannot be parsed, another schema, an unknown producer: not_available naming it, never guessed at", y_unrecognised)
    case("Y3 no report and no manuscript yet: figure_unused is not_available, the other checks are judged (assumed: A7)", y_reports)
    case("Y4 no manifest, or a manifest hash that is no hash: never a match", y_manifest)
    case("Y5 a provenance record missing, malformed or with a hash that is no hash: not_available, and a stale source still a problem", y_records)
    case("Y6 the view only reads: no file of the run changes", y_reads_only)
    case("W1 no domain word in this file (§3a)", w_source)
    case("W2 no domain word in the view's result, for either producer (§3a)", w_words)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: figures.py --self-test")
