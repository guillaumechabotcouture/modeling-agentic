#!/usr/bin/env python3
"""The Fit view's chart declarations (docs/design/review-workbench.md §5.5, chart keys in workbench.py).

    python3 tools/workbench/views/fit_charts.py --self-test

`charts(m)` takes the generic Fit model (views/fit_adapters.py) and returns chart declarations only: the page is
generic and draws them from the model's tables. It also adds the derived table `groups` to the model (the targets'
signed scores added up by group), which one of the charts reads. A chart is declared only for a table the method
records; the words for each mark come from the adapter.
"""
from __future__ import annotations

import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
from views.fit_adapters import GROUP_BIAS, GROUP_COLUMNS, NEAR, _dicts, _g, groups as group_rows  # noqa: E402


def charts(m):
    t, w, f, out = m["tables"], m["words"], m["facts"], []
    rows = _dicts(t["targets"])
    def fit_chart(level, x, more, tail):
        of = [r for r in rows if r["level"] == level]
        alt = any(r["model_alt"] is not None for r in of)
        second = (f"; a hollow diamond is the {w['model_alt']}, on whose values the fit was accepted" if alt else
                  f"; the {w['model_alt']} is not recorded for these targets (its score is the column score_alt)"
                  if w["model_alt"] and any(r["score_alt"] is not None for r in of) else "")
        return {"kind": "interval_points", "table": "targets", "x": x, "y": "observed", "low": "low", "high": "high",
                "model": "model", **({"model_alt": "model_alt"} if alt else {}), "where": {"level": level}, **more,
                "labels": {"observed": w["observed"], "model": w["model"], **({"model_alt": w["model_alt"]} if alt else {})},
                "title": f"Observed against model: the {len(of)} {level} targets" + (f", by {more['panels']}" if more else ""),
                "how_to_read": f"Each row is one target. The dot is the {w['observed']} and the bar through it is "
                               f"{w['interval']}. A filled diamond is the {w['model']}{second}. A diamond outside the "
                               f"bar is a target the model misses by more than that interval; diamonds all on one "
                               f"side of their dots within a group mean the model is biased for that group. {tail}"}
    if any(r["level"] == "group" for r in rows):
        out.append(fit_chart("group", "group", {}, "Rows are in order of the largest score first."))
    if any(r["level"] == "unit" for r in rows):
        out.append(fit_chart("unit", "id", {"panels": w["panels"]}, f"One panel per {w['panels']}, each on its own axis; "
                             f"within a panel the rows are in the table's order."))
    if rows and f["cutoff"] is None:
        out.append({"kind": "bars", "table": "targets", "x": "id", "y": "score", "labels": {"score": w["score"]},
                    "limits": [{"value": -2, "label": "−2"}, {"value": 2, "label": "+2"}],
                    "title": f"Score by target: {w['score']}",
                    "how_to_read": f"One bar per target: the {w['scored']}. A bar to the right means the model is above "
                                   "the observation. The lines at ±2 are a reading aid for a standardised residual, "
                                   "not a threshold of the method."})
    if f["cutoff"] is not None and any(r["level"] == "group" and r["score"] is not None for r in rows):
        cut = f["cutoff"]
        out.append({"kind": "bars", "table": "targets", "x": "group", "y": "score", "where": {"level": "group"},
                    "labels": {"score": w["score"]},
                    "limits": [{"value": (1 - NEAR) * cut, "label": _g((1 - NEAR) * cut)}, {"value": cut, "label": f"cutoff {_g(cut)}"}],
                    "title": f"Score of the group targets against the method's cutoff: {w['score']}",
                    "how_to_read": f"One bar per group target: the {w['score']} (unsigned). A bar beyond the cutoff "
                                   f"line failed; a bar between the two dashed lines passed within 25% of the cutoff, which "
                                   f"this view reports as a near miss. The unit targets' scores are in the targets table."})
    groups = [r for r in group_rows(m) or [] if r[1] >= 2]
    if groups:
        m["tables"]["groups"] = {
            "columns": GROUP_COLUMNS, "rows": sorted(groups, key=lambda r: -abs(r[2])),
            "about": f"Derived here from the targets table: one row per group with two or more scored targets. "
                     f"mean_signed_score is the mean of the {w['scored']}; largest_score the signed score furthest "
                     f"from zero; model_above and model_below count the targets on each side.",
            "labels": {"mean_signed_score": "workbench threshold ±1"}}
        out.append({"kind": "bars", "table": "groups", "x": "group", "y": "mean_signed_score",
                    "labels": {"mean_signed_score": "mean signed score of the group's targets"},
                    "limits": [{"value": -GROUP_BIAS, "label": "−1"}, {"value": GROUP_BIAS, "label": "+1 (workbench threshold)"}],
                    "title": "Residuals by group: mean signed score",
                    "how_to_read": f"One bar per group: the mean, over the group's targets, of the {w['scored']}. A bar "
                                   "to the right means the model sits above that group's targets on average, to the "
                                   "left below. A bar beyond a line is a group the group_bias check names."})
    if t["changes"] is not None and t["changes"]["rows"]:
        lim = f["change_limit"]
        out.append({"kind": "interval_points", "table": "changes", "x": "group", "y": "observed", "model": "modelled",
                    "labels": {"observed": f"observed {w['change']}", "modelled": f"modelled {w['change']} (engine at the best point)"},
                    "limits": [{"value": 0, "label": "no change"}],
                    "title": "Change between rounds, observed against modelled, by group",
                    "how_to_read": f"Each row is one group. The dot is the observed {w['change']} (later minus earlier) "
                                   "and the diamond the modelled one. A dot and a diamond on opposite sides of the "
                                   "'no change' line mean the model moves the wrong way for that group; z in the "
                                   "next chart says how far apart they are in standard deviations."})
        out.append({"kind": "bars", "table": "changes", "x": "group", "y": "z", "labels": {"z": "z = (modelled − observed change) ÷ sd"},
                    "limits": [] if not lim else [{"value": -lim, "label": f"−{_g(lim)}"}, {"value": lim, "label": f"+{_g(lim)}"},
                                                  {"value": -(1 - NEAR) * lim, "label": f"−{_g((1 - NEAR) * lim)}"},
                                                  {"value": (1 - NEAR) * lim, "label": f"+{_g((1 - NEAR) * lim)}"}],
                    "title": "Change between rounds: z by group, against the method's limit",
                    "how_to_read": "One bar per group: z, the modelled change minus the observed change in units of its "
                                   f"standard deviation. The method flags a group beyond its limit (the outer dashed lines, "
                                   f"±{_g(lim)}); the inner dashed lines mark 25% inside the limit, where this view reports "
                                   "a near miss. A bar "
                                   "to the left means the model fell more, or rose less, than the observations."})
    if t["parameters"] is not None and t["parameters"]["rows"]:
        out.append({"kind": "ranges", "table": "parameters", "x": "name", "outer_low": "prior_low", "outer_high": "prior_high",
                    "low": "low", "high": "high", "y": "best",
                    "labels": {"prior_low": "prior range", "low": w["accepted"], "best": "best point"},
                    "title": f"{w['accepted'].capitalize()} against the prior range, per parameter",
                    "how_to_read": f"Each row is one parameter on its own scale: the grey track is the prior range, the "
                                   f"blue bar the {w['accepted']}, the diamond the best point. A bar that fills the "
                                   "track means the fit did not narrow the parameter; a bar or diamond touching an end "
                                   "of the track means the fit pushes against the prior's bound (the at_bound column)."})
    if t["offsets"] is not None and t["offsets"]["rows"]:
        out.append({"kind": "bars", "table": "offsets", "x": "key", "y": "mean",
                    "labels": {"mean": "mean accepted offset, on the target's scale"},
                    "title": "Offsets: the mean accepted shift, per source and group",
                    "how_to_read": "One bar per offset: the mean, over the accepted draws, of the common shift applied "
                                   "to one source's unit targets in one group. A long bar means the fit needs that "
                                   "source to be off by that much in that group; compare it with the offset's prior sd "
                                   "and with accepted_share in the offsets table."})
    if t["series"] is not None and t["series"]["rows"]:
        out.append({"kind": "lines", "table": "series", "x": "t", "y": "observed", "model": "model", "low": "low",
                    "high": "high", "group": "series",
                    "labels": {"observed": "observed", "model": "predictive median", "low": "predictive band", "t": "step"},
                    "title": "Fit over time: observed against the predictive band",
                    "how_to_read": "Each dot is an observed value at one step; the line is the predictive median and "
                                   "the shaded band the predictive interval. Dots outside the band, or a run of dots "
                                   "all above or all below the line, are where the model does not follow the series."})
    return out


# §5.12, figure to view: the calibration figures and the posterior diagnostics belong beside Fit. Each chart below is
# shown with the harness figure of the same data where the run has one: (kind, table, the level its rows are limited
# to or None) and the figure names in order of preference. A figure is shown beside one chart only.
BESIDE = ((("interval_points", "targets", "group"), ("calibration_fit",)),
          (("interval_points", "targets", "unit"), ("calibration_unit_fit", "calibration_fit")),
          (("bars", "targets", None), ("calibration_residuals",)),
          (("bars", "groups", None), ("calibration_residuals",)),
          (("ranges", "parameters", None), ("calibration_region",)),
          (("lines", "series", None), ("diagnostics_ppc",)))


def beside(charts, figures):
    """The charts, each with `image` ({path, hash, title, caption}: the chart key of workbench.py) where `figures`
    (checks.harness_figures) holds the harness figure of the same data."""
    used = set()
    for (kind, table, level), names in BESIDE:
        ch = next((c for c in charts if (c["kind"], c["table"]) == (kind, table) and "image" not in c
                   and (level is None or (c.get("where") or {}).get("level") == level)), None)
        name = next((n for n in names if n in (figures or {}) and n not in used), None)
        if ch is not None and name is not None:
            used.add(name)
            f = figures[name]
            ch["image"] = {"path": f["path"], "hash": f["sha256"], "title": f["title"] or name.replace("_", " "),
                           **({"caption": f["caption"]} if f["caption"] else {})}
    return charts


# ---------------------------------------------------------------------------------------------- self-test

def _self_test():
    from views import fit
    from views.fit_bench import Bench, need
    b = Bench(fit.view)
    folder, look_at, case = b.folder, b.look_at, b.case
    hm, post = "history_matching", "posterior"

    def f10():
        import workbench
        wanted = {hm: {("interval_points", "targets"), ("ranges", "parameters"), ("interval_points", "changes")},
                  post: {("interval_points", "targets"), ("ranges", "parameters"), ("lines", "series")}}
        for method in (hm, post):
            v = look_at(folder(method))
            need(isinstance(v["charts"], list) and v["charts"], f"{method}: at least one chart")
            bad = workbench.chart_problems(v)
            need(not bad, f"{method}: every chart has a known kind and keys, a title, a how-to-read sentence, and "
                          f"names an existing table and its columns ({bad})")
            need(all(len(ch["how_to_read"]) > 80 for ch in v["charts"]), f"{method}: each chart says how to read it")
            kinds = {(ch["kind"], ch["table"]) for ch in v["charts"]}
            need(wanted[method] <= kinds, f"{method}: the charts of §5.5 are declared ({sorted(wanted[method] - kinds)} missing)")
        fit_charts = [ch for ch in look_at(folder(hm))["charts"] if (ch["kind"], ch["table"]) == ("interval_points", "targets")]
        need({ch.get("where", {}).get("level") for ch in fit_charts} == {"group", "unit"},
             "history matching: group targets and unit targets are drawn apart")
        need(all(ch.get("model") == "model" and "engine" in ch["labels"]["model"] and ch.get("low") == "low"
                 and ch.get("high") == "high" for ch in fit_charts), "observed with its interval and the engine's value, named as the engine's")
        by = {ch["where"]["level"]: ch for ch in fit_charts}
        need(by["group"].get("model_alt") == "model_alt" and "emulator" in by["group"]["labels"]["model_alt"],
             "group targets: the emulator's value is drawn too, named as the emulator's")
        need("model_alt" not in by["unit"] and "not recorded" in by["unit"]["how_to_read"],
             "unit targets: no emulator mark is declared where the file records none, and the chart says so")
        need(not any(ch["table"] == "series" for ch in look_at(folder(hm))["charts"]), "no chart of a table the method does not record")

    def f_images():
        import hashlib
        import workbench

        def sha(d, rel):
            with open(os.path.join(d, rel), "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()

        def image(v, kind, table, level=None):
            return next(ch for ch in v["charts"] if (ch["kind"], ch["table"]) == (kind, table) and (level is None or ch["where"]["level"] == level)).get("image")
        d = folder(hm)
        v = look_at(d)
        for (kind, table, level), name in ((("interval_points", "targets", "group"), "calibration_fit"), (("interval_points", "targets", "unit"), "calibration_unit_fit"),
                                           (("bars", "targets", None), "calibration_residuals")):
            got = image(v, kind, table, level)
            need(isinstance(got, dict) and got.get("path") == f"figures/{name}.png" and got.get("hash") == sha(d, f"figures/{name}.png") and got.get("title"),
                 f"the {kind} chart of {table} {level or ''} shows the harness figure {name} beside it, by the hash of the image as read ({got})")
        need(image(v, "interval_points", "changes") is None and image(v, "bars", "groups") is None,
             "a chart with no harness figure of the same data, or whose figure is beside another chart, carries none")
        used = [ch["image"]["path"] for ch in v["charts"] if ch.get("image")]
        need(len(used) == len(set(used)) and not workbench.chart_problems(v), f"no figure is shown twice, and every declaration is one the page can draw ({used})")
        d = folder(post, figures="estimates")
        got = image(look_at(d), "lines", "series")
        need(isinstance(got, dict) and got.get("path") == "figures/diagnostics_ppc.png" and got.get("hash") == sha(d, "figures/diagnostics_ppc.png"),
             f"the fit over time shows the harness figure of the predictive check ({got})")
        gone = b.edited(hm, lambda folder_: os.remove(os.path.join(folder_, "figures", "calibration_fit.png")))
        need(image(look_at(gone), "interval_points", "targets", "group") is None, "a figure whose image is not in the run is not declared")
        none = b.edited(hm, lambda folder_: os.remove(os.path.join(folder_, "analysis", "figures.yaml")))
        need(not any(ch.get("image") for ch in look_at(none)["charts"]), "a run with no figure index: the charts are drawn with no figure beside them")

    case("H1 every chart declaration names an existing table and its columns", f10)
    case("H2 a chart carries the harness figure of the same data where the run has one (§5.12: the calibration figures belong beside Fit)", f_images)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: fit_charts.py --self-test")
