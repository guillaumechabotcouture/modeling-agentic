"""Uncertainty over the declared sources only, labelled with what it covers (spike lesson 6).

Each draw samples the spec's `uncertainty.sources`, in this order (the spike's order for
its two sources, so the stored spike batch reproduces):
  engine_parameters  every fitted engine parameter, uniform within its engine-card range
  calibration        emulator_inversion: a per-region common-mode shift of the logit target
                     (sd `calibration.target_uncertainty.region_sd_logit`), then re-inversion;
                     likelihood: (calibrated parameters, unit attributes) from N(estimate, covariance);
                     posterior: rows of the canonical joint posterior sample (v2d §3.3: each draw records the
                     sample id, its row and the values applied, re-checked against the sample, fail closed)
  costs              a uniform multiplier per listed cost item
  emulator           one joint draw of the errors of every unit: a common (population-mean) component plus a
                     per-unit one, from the held-out residuals (toolkit/analysis/model.py EmulatorError), for every
                     emulated quantity the decision uses: reductions, comparator burden and cost outputs (F6 S2-4),
                     drawn from the same residual rows so they keep their held-out correlation
  priors             (v2 priors, drawn whenever there is one) every prior parameter from its declared bounded
                     distribution (toolkit/spec/priors.py), on its own random stream, before calibration
  unit_rates         (hb3 fix 5; an engine that declares per-unit sampling, direct evaluation) each unit's measured
                     attributes from the engine's own sampling model (engine.draw_unit_attributes), on its own
                     stream, independently across units; the draws reach the engine through DirectModel.unit_spec
Per-unit values drawn by either route (unit_rates, or the calibration covariance's z of likelihood, posterior and joint
calibration) are summarised in the report's `unit_values_drawn`, so a report never reads as if they were held fixed
(hb3 malaria R1: `sampler_varied_parameters` counted the 6 rate ratios, not the 10 district rates drawn with them).
The point plan is evaluated in every draw (the headline interval) and the plan is
re-optimised in every draw (package stability). The point plan's cost is recorded in every draw
too (F6 S1-1): costs move with treated cases and unit prices, so a plan within the budget at the
point values can exceed it in a draw. `affordability` reports the share of draws over the
allocatable budget and the cost quantiles, and checks the spec's `uncertainty.affordability`
rule (max_share_over_budget) when one is declared. With `enforce: true` (S1-1b) the analysis first evaluates these
draws once (`evaluate_draws`) and `reserve_search` finds the largest budget fraction whose plan meets the rule; run()
then reads the same evaluations (`pre=`). The interval's label comes from the spec
(`Spec.uncertainty_scope`): what it covers and what it does not. Writes
`uncertainty_report.yaml` in the schema of scripts/propagate_uncertainty.py (shared
summariser), plus the scope and the draw plan: every source drawn or not, every engine
parameter drawn or held fixed with why, and the emulator-error model with its held-out
evidence, so a report cannot claim more precision than the draws carry.
run() also returns `plan_draws`: each draw's fixed-plan total of the primary outcome and its per-package sums (units,
cost, averted), which the figure step reads on direct evaluation instead of re-running the engine (hb1 flu R2: the
re-run took as long as the draws). Long per-draw loops log a progress line with an ETA (Progress).
"""
from __future__ import annotations

import math
import os
import sys
import time
from collections import Counter
from statistics import mean, pstdev

import numpy as np
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.analysis.core import q  # noqa: E402
PRODUCER = "toolkit/analysis/uncertainty.py"
ORDER = ("engine_parameters", "calibration", "costs", "emulator")
POINT_TOL = 1e-6           # v2-posterior-point: a value within the fit file's 6-decimal rounding of the chosen point
PROGRESS_EVERY_S = 300.0    # a long per-draw loop logs its progress at most this often (and at its last item)
NE_STREAM = 0x6E6566        # the net-effect posterior's row choice: its own stream, the other sources' draws unchanged
UNIT_STREAM = 0x756E74      # unit_rates: its own stream, so declaring it leaves every other source's draws unchanged
HEALTH_WEIGHT_STREAM = 0x687774   # health_weights (CEA): its own stream so other sources' draws are unchanged


def posterior_input(spec) -> dict | None:
    """{path, sha256, data} of the spec's uncertainty.net_effect_posterior {path, sha256}: the path resolved against
    the spec, the owner-signed sha256 and the file's bytes, read once per spec and refused unless their sha256 is the
    signed one (Codex r1 P1-1, P2-4). The manifest, the engine artifact's input and the Drawer all use these bytes.
    Optional `margin` (default 0): the share of the sample's hull width each side of its columns' ranges is widened by
    (net_effect_sample). None without the field; a field left empty is refused (Codex r1 P1-6, v2-posterior-point)."""
    u = spec.raw.get("uncertainty") or {}
    if "net_effect_posterior" not in u:
        return None
    p = u["net_effect_posterior"]
    if not isinstance(p, dict) or not isinstance(p.get("path"), str) or not p["path"].strip() \
            or not isinstance(p.get("sha256"), str) or set(p) - {"path", "sha256", "margin", "exception"}:
        raise ValueError("uncertainty.net_effect_posterior: {path: net_effect_fit.py's output file, sha256: its "
                         "signed digest, margin (optional): a share of the sample's hull width in [0, 1], exception "
                         "(optional): an owner-signed acceptance of recorded verification failures}")
    exc = posterior_exception(spec)
    m = p.get("margin", 0.0)
    if isinstance(m, bool) or not isinstance(m, (int, float)) or not np.isfinite(m) or not 0 <= m <= 1:
        raise ValueError(f"uncertainty.net_effect_posterior.margin {m!r}: a share of the sample's hull width in [0, 1]")
    path, dig = spec.resolve(p["path"]), p["sha256"].lower()
    got = getattr(spec, "_net_effect_posterior", None)
    if got is None or (got["path"], got["sha256"]) != (path, dig):
        from toolkit.engines import effect_fit as EF
        got = {"path": path, "sha256": dig, "data": EF.read_signed(path, dig)}
        spec._net_effect_posterior = got
    return dict(got, margin=float(m), exception=exc)


EXCEPTION_KEYS = ("failed_points", "rule", "evidence", "reason", "draws")


def posterior_exception(spec) -> dict | None:
    """uncertainty.net_effect_posterior.exception (owner, 2026-11-20): the owner-signed acceptance of a net-effect
    sample whose verification recorded failures. {failed_points: [{point: {parameter: value}, targets: [id, ...]}],
    rule: the rule they fail, evidence: what else was verified, reason, draws: keep | exclude (the failed points in or
    out of the draws)}. The loader accepts the sample only when it matches the file's recorded failures exactly
    (net_effect_fit.load_posterior). None without it; a malformed one is refused (ValueError)."""
    e = ((spec.raw.get("uncertainty") or {}).get("net_effect_posterior") or {}).get("exception")
    if e is None:
        return None
    pts = e.get("failed_points") if isinstance(e, dict) else None
    good = (isinstance(e, dict) and set(e) == set(EXCEPTION_KEYS) and isinstance(pts, list) and pts
            and all(isinstance(e[k], str) and e[k].strip() for k in ("rule", "evidence", "reason"))
            and e["draws"] in ("keep", "exclude")
            and all(isinstance(x, dict) and set(x) == {"point", "targets"} and isinstance(x["point"], dict)
                    and x["point"] and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                           for v in x["point"].values())
                    and isinstance(x["targets"], list) and x["targets"]
                    and all(isinstance(t, str) and t for t in x["targets"]) for x in pts))
    if not good:
        raise ValueError(f"uncertainty.net_effect_posterior.exception: {{{', '.join(EXCEPTION_KEYS)}}}; failed_points "
                         f"a non-empty list of {{point: {{parameter: number}}, targets: [benchmark id, ...]}}, draws "
                         f"keep or exclude, the rest non-empty text (got {e!r})")
    return e


def exception_sheet_lines(raw: dict) -> list:
    """The sign-off sheet's HIGH line for a declared net-effect posterior exception (owner-signed departure)."""
    post = ((raw.get("uncertainty") or {}).get("net_effect_posterior") or {})
    e = post.get("exception") if isinstance(post, dict) else None
    if not isinstance(e, dict):
        return []
    pts = [f"{', '.join(map(str, x.get('targets') or []))} at {x.get('point')}" for x in e.get("failed_points") or []
           if isinstance(x, dict)]
    return ["## Net-effect posterior exception (owner-signed departure)",
            f"- **HIGH**: the net-effect posterior `{post.get('path')}` (sha256 {str(post.get('sha256'))[:12]}…) is "
            f"accepted although its verification failed ({e.get('rule')}): {'; '.join(pts)}. Evidence: "
            f"{e.get('evidence')}. Reason: {e.get('reason')}. The failed point(s) are "
            f"{'kept in' if e.get('draws') == 'keep' else 'excluded from'} the draws.", ""]


def net_effect_sample(spec, eng, fitted: dict | None = None, priors: dict | None = None, held=()) -> dict | None:
    """The spec's net-effect posterior loaded and verified (net_effect_fit.load_posterior: sha256, finalize() replay,
    seeds, engine identity, hard bounds) with `ranges` {name: (lo, hi)}: its columns' draw ranges and emulator design
    box, from net_effect_fit.posterior_ranges (the sample's hull, the spec's margin, within hard bounds; owner, 14 Nov).
    The Drawer, the analysis (core), batch.design_box and the sign-off precondition all read them here. The columns
    must be parameters the draws vary: drawn priors (`priors`, default toolkit/spec/priors.py drawn) and, under the
    engine_parameters source, fitted parameters (`fitted`, default batch.fitted_ranges), less calibrated and jointly
    calibrated ones and `held`. Cached on the spec per (sha256, margin, varied names, engine identity). None without
    the field."""
    post = posterior_input(spec)
    if post is None:
        return None
    from toolkit.engines import effect_fit as EF
    if fitted is None:
        from toolkit.analysis.batch import fitted_ranges
        fitted = fitted_ranges(eng)
    if priors is None:
        from toolkit.spec import priors as PRI
        priors = PRI.drawn(spec)
    src = {s.get("kind") for s in (spec["uncertainty"].get("sources") or []) if isinstance(s, dict)}
    cal = spec["calibration"]
    held = set(held) | set(cal.get("parameters") or []) | set((cal.get("joint") or {}).get("shared") or [])
    varied = {k: (p.lo, p.hi) for k, p in priors.items()}
    if "engine_parameters" in src:
        varied.update({k: tuple(v[:2]) for k, v in fitted.items()})
    varied = {k: v for k, v in varied.items() if k not in held}
    from toolkit.engines.identity import engine_identity      # Codex r1 P2-3: the engine is part of what was verified
    import json as _json
    key = (post["sha256"], post["margin"], tuple(sorted(varied)), tuple(sorted(engine_identity(eng).items())),
           _json.dumps(post.get("exception"), sort_keys=True))
    cache = getattr(spec, "_net_effect_sample", None)
    if cache is not None and cache[0] == key:
        return cache[1]
    ne = EF.load_posterior(post["path"], eng, varied, post["sha256"], post["data"], exception=post.get("exception"))
    ne["margin"] = post["margin"]
    ne["ranges"] = EF.posterior_ranges(ne, post["margin"], EF.hard_bounds(eng, ne["names"]))
    ne["point"] = chosen_point(post, ne["names"])
    spec._net_effect_sample = (key, ne)
    return ne


def chosen_point(post: dict, names) -> dict:
    """{column: value} of the fit's verified chosen point (joint_region.verified.chosen.point in the signed bytes that
    load_posterior replayed): the point value of every posterior column (v2-posterior-point). Refused (fail closed; no
    fallback to card or fitted values): a file without one; a point whose coordinates are not exactly the sample's
    columns, or hold a value that is not a finite number; a point that is not exactly the fit's recorded `parameters`
    values."""
    import yaml
    from toolkit.engines import effect_fit as EF
    doc = yaml.safe_load(post["data"])
    pt = ((((doc.get("joint_region") or {}).get("verified") or {}).get("chosen") or {}).get("point"))
    if not isinstance(pt, dict) or sorted(pt) != sorted(names) or any(
            isinstance(pt[k], bool) or not isinstance(pt[k], (int, float)) or not np.isfinite(pt[k]) for k in names):
        raise EF.PosteriorRefused(f"net-effect posterior {post['path']}: no verified chosen point whose coordinates are "
                                   f"the sample's columns {list(names)}, each a finite number "
                                   f"(joint_region.verified.chosen.point: {pt!r})")
    # run() writes the chosen point from its fit's parameter values: they must be the same numbers (Codex r1 P1-4, r2)
    fp = doc.get("parameters") if isinstance(doc.get("parameters"), dict) else {}
    off = sorted(set(fp) - set(names)) + [      # a recorded parameter the sample lacks (Codex r3), or another value
        k for k in names if not isinstance(fp.get(k), dict) or isinstance(fp[k].get("value"), bool)
        or not isinstance(fp[k].get("value"), (int, float)) or not float(fp[k]["value"]) == float(pt[k])]
    if off:
        raise EF.PosteriorRefused(f"net-effect posterior {post['path']}: the chosen point of {off} is not the fit's "
                                   f"recorded parameter values (parameters: {[fp.get(k) for k in off]}; chosen point: "
                                   f"{[pt.get(k) for k in off]})")
    return {k: float(pt[k]) for k in names}


def posterior_point(spec, ne: dict | None, theta: dict) -> dict:
    """`theta` with every posterior column at the fit's chosen point (core.Analysis.theta0, joint_calibration.theta0).
    A spec engine_parameters value of a column that differs from it by more than POINT_TOL (the fit file's 6-decimal
    rounding, whatever the magnitude: Codex r1 P1-5) is refused. So is a likelihood or posterior calibration, whose
    engine parameters are the calibrated ones over engine defaults, never this point (Codex r1 P1-1). `theta`
    unchanged without a posterior."""
    if ne is None:
        return theta
    m = (spec.raw.get("calibration") or {}).get("method")
    if m in ("likelihood", "posterior"):
        raise ValueError(f"uncertainty.net_effect_posterior with calibration.method {m}: that calibration evaluates the "
                         "engine at its default values, not at the posterior's chosen point; not supported")
    from toolkit.spec import engine_parameters as PV
    sv = PV.values(spec)
    # the float subtraction's own error (a few ulps at the values' magnitude) is not a difference (Codex r2-r3)
    bad = {k: (sv[k], v) for k, v in ne["point"].items() if k in sv
           and abs(sv[k] - v) > POINT_TOL + 4 * float(np.spacing(max(abs(sv[k]), abs(v))))}
    if bad:
        raise ValueError("engine_parameters: " + "; ".join(
            f"{k} is set to {a}, but the net-effect posterior's verified chosen point is {b}; the chosen point is the "
            f"point value of a posterior column: set the spec value to it or remove the value" for k, (a, b) in
            sorted(bad.items())))
    return dict(theta, **ne["point"])


def point_source(ne: dict | None) -> dict:
    """The manifest's record of where each posterior column's point value came from ({} without a posterior)."""
    return {k: "posterior_chosen_point" for k in (ne or {}).get("point", {})}


def with_posterior_ranges(ne: dict | None, fitted: dict, priors: dict | None = None) -> tuple:
    """(fitted, priors) copies with each posterior column's range replaced by net_effect_sample's `ranges`: fitted
    {name: (lo, hi, value, ...)} keeps its value, priors {name: Prior} keep everything but lo and hi. Unchanged objects
    without a posterior; columns not in the sample are untouched."""
    if ne is None:
        return fitted, priors
    import dataclasses
    rg = ne["ranges"]
    fr = {k: ((*rg[k], *v[2:]) if k in rg else v) for k, v in fitted.items()}
    # a replaced prior is drawn as uniform over the hull (Codex r1 P2-1: a log-uniform over a hull reaching zero is
    # undefined); every prior kind takes one uniform from the stream and the value is replaced by the sample's row
    pr = None if priors is None else {k: (dataclasses.replace(p, lo=rg[k][0], hi=rg[k][1], dist="uniform")
                                          if k in rg else p) for k, p in priors.items()}
    return fr, pr


def posterior_range_record(ne: dict) -> dict:
    """What the manifest and the uncertainty report record of the ranges the sample set."""
    return dict({"sha256": ne["sha256"], "range_source": "posterior_hull", "margin": ne["margin"],
                 "ranges": {k: [float(lo), float(hi)] for k, (lo, hi) in ne["ranges"].items()}},
                **({"exception": ne["exception"]} if ne.get("exception") else {}))


def parameter_summary(per_param_samples: dict) -> dict:
    """The report's `parameter_samples:` block (mean, std, 2.5% / 97.5% order statistics)."""
    out = {}
    for name, samples in per_param_samples.items():
        samples_sorted = sorted(samples)
        n = len(samples_sorted)
        if not n:
            continue
        out[name] = {
            "mean": float(mean(samples)),
            "std": float(pstdev(samples)) if n > 1 else 0.0,
            "ci_low": float(samples_sorted[int(0.025 * n)]),
            "ci_high": float(samples_sorted[min(n - 1, int(0.975 * n))]),
        }
    return out


RARE_EVENT_ZERO_FRACTION = 0.5


def rare_event_summary(vals: list[float]) -> dict:
    """p_nonzero, its binomial standard error and the conditional mean for a zero-inflated output."""
    n = len(vals)
    nonzero = [v for v in vals if v != 0.0]
    if not n or not nonzero or (n - len(nonzero)) / n < RARE_EVENT_ZERO_FRACTION:
        return {}
    p = len(nonzero) / n
    return {"rare_event": True,
            "p_nonzero": p,
            "p_nonzero_se": math.sqrt(p * (1.0 - p) / n),
            "mean_if_nonzero": float(mean(nonzero))}


def interval_quantiles(level: float = 0.95) -> tuple[float, float]:
    """Lower and upper quantiles of a central interval."""
    if level == 0.95:
        return 0.025, 0.975
    if not 0.5 <= float(level) < 1:
        raise ValueError(f"interval level {level!r} must be in [0.5, 1)")
    a = round((1.0 - float(level)) / 2.0, 12)
    return a, round(1.0 - a, 12)


def summarise_outputs(outs: list, per_param_samples: dict, n_draws: int, level: float = 0.95,
                      summary=None) -> dict:
    """Aggregate per-draw outcome dicts into scalar_outputs, categorical_outputs, parameter_samples."""
    qlo, qhi = interval_quantiles(level)
    errors: list[dict] = []
    scalar_results: dict[str, list[float]] = {}
    categorical_results: dict[str, list[str]] = {}
    n_nonfinite: dict[str, int] = {}
    for i, out in enumerate(outs):
        if isinstance(out, dict) and "_error" in out:
            errors.append(out["_error"])
            continue
        if not isinstance(out, dict):
            errors.append({"draw": i, "exception": "TypeError",
                           "message": f"outcome_fn must return dict, "
                                      f"got {type(out).__name__}"})
            continue
        for key, val in out.items():
            if isinstance(val, bool):
                categorical_results.setdefault(key, []).append(str(val))
            elif isinstance(val, (int, float)):
                if math.isfinite(val):
                    scalar_results.setdefault(key, []).append(float(val))
                else:
                    n_nonfinite[key] = n_nonfinite.get(key, 0) + 1
            elif isinstance(val, str):
                categorical_results.setdefault(key, []).append(val)

    if errors and len(errors) / max(1, n_draws) > 0.2:
        raise RuntimeError(
            f"outcome_fn errors on >20% of draws "
            f"({len(errors)}/{n_draws}). Last error: "
            f"{errors[-1]}. Aborting."
        )

    parameter_samples = parameter_summary(per_param_samples)
    varied_params = [n for n, ps in parameter_samples.items() if ps["std"] > 0]

    scalar_summary = {}
    degenerate_outputs: list[str] = []
    for key, vals in scalar_results.items():
        if not vals:
            continue
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        if summary is not None:
            est = summary(vals, level)
            entry = {k: float(est[k]) for k in ("mean", "median", "ci_low", "ci_high")}
        else:
            entry = {
                "mean": float(mean(vals)),
                "median": float(vals_sorted[n // 2]),
                "ci_low": float(vals_sorted[int(qlo * n)]),
                "ci_high": float(vals_sorted[min(n - 1, int(qhi * n))]),
            }
        entry.update(n=n, n_nonfinite=n_nonfinite.get(key, 0))
        entry.update(rare_event_summary(vals))
        scale = max(abs(entry["mean"]), 1e-300)
        if n > 1 and (vals_sorted[-1] - vals_sorted[0]) / scale < 1e-12:
            entry["degenerate"] = True
            entry["degenerate_diagnostic"] = (
                "every draw produced the same value although "
                f"{len(varied_params)} sampled parameter(s) varied — "
                "outcome_fn likely ignores its `params` argument (e.g. reads "
                "module-level constants or a cached result)"
                if varied_params else
                "every draw produced the same value AND no sampled parameter "
                "varied — the registry priors have zero width or the sampler "
                "returned constants")
            degenerate_outputs.append(key)
        scalar_summary[key] = entry
    for key, cnt in n_nonfinite.items():
        if key not in scalar_summary:
            scalar_summary[key] = {"n": 0, "n_nonfinite": cnt,
                                   "degenerate": True,
                                   "degenerate_diagnostic":
                                       "all draws were NaN/inf"}
            degenerate_outputs.append(key)

    categorical_summary = {}
    for key, vals in categorical_results.items():
        counts = Counter(vals)
        dominant, dominant_n = counts.most_common(1)[0]
        categorical_summary[key] = {
            "counts": dict(counts),
            "dominant": dominant,
            "dominance": dominant_n / len(vals),
            "n": len(vals),
        }

    return {"errors": errors, "scalar_outputs": scalar_summary, "categorical_outputs": categorical_summary,
            "parameter_samples": parameter_samples, "degenerate_outputs": degenerate_outputs,
            "varied_params": varied_params}


class Drawer:
    """Draws the declared sources; `draw()` returns (theta, zs, noise, cost multipliers, sampled values)."""

    def __init__(self, A, seed):
        self.A = A
        sp = A.spec["uncertainty"]
        self.src = {s["kind"]: s for s in sp.get("sources") or []}
        self.rng = np.random.default_rng(int(seed))
        from toolkit.spec.priors import PRIOR_STREAM
        self.prng = np.random.default_rng([int(seed), PRIOR_STREAM])     # priors: their own stream
        if "emulator" in self.src and getattr(A.model, "error", None) is None:
            raise ValueError("uncertainty: the emulator source is declared but the emulator has no error model (fewer "
                             "than 3 held-out points); raise analysis.emulator.holdout_share or exclude it with a reason")
        cal = A.spec["calibration"]
        self.sd = float((cal.get("target_uncertainty") or {}).get("region_sd_logit", 0.0))
        self.regions = sorted({u[A.region] for u in A.spec.units()}) if A.region else []
        if A.method == "likelihood" and "calibration" in self.src:
            if A.lik.get("cov") is None:
                raise ValueError("uncertainty: the calibration covariance is singular; the calibration source cannot "
                                 "be drawn (see analysis/calibration.yaml identifiability)")
            self.mvn = (np.array([A.lik["x"][n] for n in A.lik["names"]]), np.array(A.lik["cov"]))
        self.seed = int(seed)
        self.post_draws, self.records = None, []
        self.jc_draws, self.jc_records = None, []
        # joint calibration: its shared parameters are no longer free priors; with the calibration source they are
        # drawn from the region sample, without it they stay at the region's best point
        self.jc_names = set((A.joint or {}).get("shared") or [])
        from toolkit.analysis import burden as BU      # F6 S2-5d review r1 P2-3: the anchors' external estimates
        self.arng = np.random.default_rng([int(seed), BU.ANCHOR_STREAM]) if BU.ANCHOR_SOURCE in self.src else None
        if self.arng is not None and getattr(A, "anchor", None) is None:
            raise ValueError(f"uncertainty: source {BU.ANCHOR_SOURCE} is declared but the analysis has no anchor")
        from toolkit.spec.schema import UNIT_RATES
        self.urng, self.last_units, self.unit_first, self.unit_varied = None, None, {}, set()
        if UNIT_RATES in self.src:
            fn = getattr(A.eng, "unit_sampling", None)
            if not callable(fn) or fn() is None:
                raise ValueError(f"uncertainty: source {UNIT_RATES} is declared but the engine declares no per-unit "
                                 "sampling")
            if getattr(A.model, "kind", None) != "direct":
                raise ValueError(f"uncertainty: source {UNIT_RATES} needs direct evaluation (an emulator does not "
                                 "take drawn unit values)")
            self.unit_sampling = fn()
            self.urng = np.random.default_rng([int(seed), UNIT_STREAM])
        # uncertainty.net_effect_posterior (opt-in): every draw takes one whole row of the net-effect fit's accepted
        # joint sample for the sample's parameters, on its own stream, after the priors and fitted ranges are drawn
        # (so the other values are those drawn without it). Owner, 14 Nov (f6-posterior-ranges): the sample's hull
        # (net_effect_sample) sets its columns' draw ranges; the other ranges are the priors' and the fitted ones
        self.ne, self.ne_rows = None, []
        self.ne = net_effect_sample(A.spec, A.eng, fitted=A.fr, priors=A.pr, held=self.jc_names)
        fr_, pr_ = with_posterior_ranges(self.ne, A.fr, A.pr)
        self.ranges = dict({k: (p.lo, p.hi) for k, p in pr_.items()},
                           **({k: tuple(v[:2]) for k, v in fr_.items()} if "engine_parameters" in self.src else {}))
        if self.ne is not None:
            self.ne_rng = np.random.default_rng([int(seed), NE_STREAM])
        self.hwrng, self.hw_items = None, []
        if "health_weights" in self.src:
            ce = A.spec.raw.get("cost_effectiveness") or {}
            hm = ce.get("health_measure") or {}
            hkind = hm.get("kind")
            wkey = "yld" if hkind == "dalys" else "qaly"
            for oc in sorted(hm.get("events") or {}):
                cfg = hm["events"][oc]
                if isinstance(cfg, dict) and not cfg.get("death") and isinstance(cfg.get(wkey), dict):
                    rng = cfg[wkey].get("range")
                    if isinstance(rng, (list, tuple)) and len(rng) == 2:
                        self.hw_items.append((str(oc), float(rng[0]), float(rng[1])))
            self.hwrng = np.random.default_rng([int(seed), HEALTH_WEIGHT_STREAM])

    def posterior_provenance(self) -> dict | None:
        """The sample's path, sha256 and names, and the row index each draw took (None without the field)."""
        if self.ne is None:
            return None
        return dict({"path": self.ne["path"], "sha256": self.ne["sha256"], "names": list(self.ne["names"]),
                     "rows": list(self.ne_rows)}, **posterior_range_record(self.ne))

    def prepare(self, nd):
        from toolkit.analysis import calibration as C
        if self.A.method == "posterior" and "calibration" in self.src:
            self.post_draws = C.draws(C.sample_of(self.A.lik), nd, self.seed)
        if self.A.joint is not None and "calibration" in self.src:
            self.jc_draws = C.draws(self.A.joint["sample"], nd, self.seed)

    def draw(self):
        A = self.A
        theta, sampled = dict(A.theta0), {}
        shifts, zs, noise, mult = None, None, None, None
        for k, p in A.pr.items():
            v = p.sample(self.prng)           # drawn even when replaced, so the other priors' stream is unchanged
            if k in self.jc_names:
                continue
            theta[k] = v
            sampled[k] = theta[k]
        if self.jc_draws is not None:
            r = self.jc_draws[len(self.jc_records)]
            for k in A.joint["shared"]:
                theta[k] = float(r["values"][k])
                sampled[k] = theta[k]
            zs = {i: float(r["values"][f"z:{i}"]) for i in A.ids}
            self.jc_records.append({"sample_id": r["sample_id"], "row": r["row"],
                                    "values": dict({k: theta[k] for k in A.joint["shared"]},
                                                   **{f"z:{i}": zs[i] for i in A.ids})})
        for kind in ORDER:
            if kind not in self.src:
                continue
            if kind == "engine_parameters":
                for k in A.fr:
                    x = float(self.rng.uniform(*self.ranges[k]))     # drawn either way: the stream is unchanged
                    if k in self.jc_names and self.jc_draws is not None:
                        continue                                # jointly calibrated: the region row's value
                    theta[k] = x
                    sampled[k] = theta[k]
            elif kind == "calibration" and A.method == "emulator_inversion" and A.joint is None:
                shifts = {r: float(self.rng.normal(0, self.sd)) for r in self.regions}
                sampled.update({f"shift:{r}": v for r, v in shifts.items()})
            elif kind == "calibration" and A.method == "likelihood":
                mu, cov = self.mvn
                x = dict(zip(A.lik["names"], self.rng.multivariate_normal(mu, cov)))
                for p in A.lik["params"]:
                    theta[p] = float(x[p])
                    sampled[p] = theta[p]
                zs = {i: float(x.get(f"z:{i}", A.lik["fixed_z"].get(i))) for i in A.ids}
            elif kind == "calibration" and A.method == "posterior":
                r = self.post_draws[len(self.records)]
                v = r["values"]
                for p in A.lik["params"]:
                    theta[p] = float(v[p]) if p in v else float(A.lik["fixed_params"][p])
                    sampled[p] = theta[p]
                fz = A.lik["fixed_z"]
                zs = {i: float(v[f"z:{i}"]) if f"z:{i}" in v else (None if fz[i] is None else float(fz[i]))
                      for i in A.ids}
                # observation targets: the sources' bias parameters are sampled jointly (nuisance), recorded with
                # the row, never passed to the engine
                applied = {**{p: theta[p] for p in A.lik["params"]}, **{f"z:{i}": zs[i] for i in A.ids},
                           **{n: float(v[n]) for n in A.lik.get("nuisance") or []}}
                self.records.append({"sample_id": r["sample_id"], "row": r["row"],
                                     "values": {n: applied[n] for n in A.lik["sample"]["names"]}})
            elif kind == "costs":
                mult = {it: float(self.rng.uniform(lo, hi)) for it, (lo, hi) in self.src["costs"]["items"].items()}
                sampled.update({f"cost:{k}": v for k, v in mult.items()})
            elif kind == "emulator":
                noise = self.rng
        if self.ne is not None:
            i = int(self.ne_rng.integers(len(self.ne["rows"])))
            for k, v in zip(self.ne["names"], self.ne["rows"][i]):
                theta[k] = sampled[k] = float(v)
            self.ne_rows.append(i)
        # per-unit values drawn with the calibration (likelihood, posterior, joint): z sampled, not re-inverted
        urow = {f"z:{i}": float(v) for i, v in (zs or {}).items() if v is not None}
        if zs is None:
            cz = A.calibrate(theta, shifts)
            zs = {i: v[0] for i, v in cz.items()}
            self.last_clamped = sorted(i for i, v in cz.items() if v[1])   # Codex r2 P1-4: kept per draw
        if self.arng is not None:
            from toolkit.analysis import burden as BU
            sampled.update(BU.draw_external(A.anchor, self.arng))
        if self.hwrng is not None:
            for oc, lo, hi in self.hw_items:
                sampled[f"hw:{oc}"] = float(self.hwrng.uniform(lo, hi))
        self.last_units = None
        if self.urng is not None:           # unit_rates: the engine's own sampling model, unit by unit in id order
            s = self.unit_sampling
            self.last_units = {}
            for i in A.ids:
                d = A.eng.draw_unit_attributes(A.model.unit_spec(i, zs.get(i)), self.urng)
                self.last_units[i] = d
                urow.update({f"{s['attribute']}:{i}:{o}": float(d[s["attribute"]][o]) for o in s["outcomes"]})
        for k, v in urow.items():           # O(keys) memory: the first value of each key, and the keys that moved
            if k not in self.unit_first:
                self.unit_first[k] = v
            elif v != self.unit_first[k]:
                self.unit_varied.add(k)
        return theta, zs, noise, mult, sampled

    def unit_values(self) -> dict:
        """The report's `unit_values_drawn`: per route, the per-unit values that varied across the draws (counted, and
        their attribute), so the report says which unit values the interval covers."""
        varied = sorted(self.unit_varied)
        out = {"n_varied": len(varied)}
        z = [k for k in varied if k.startswith("z:")]
        if z:
            out["calibration"] = {"attribute": self.A.spec["calibration"].get("attribute"), "units": len(z),
                                  "basis": "drawn jointly with the calibrated parameters (calibration source)"}
        if self.urng is not None:
            s = self.unit_sampling
            n = [k for k in varied if not k.startswith("z:")]
            out["unit_rates"] = {"attribute": s["attribute"], "outcomes": list(s["outcomes"]),
                                 # key attribute:<unit>:<outcome>; an outcome name holds no ':' (the engine refuses it)
                                 "units": len({k[len(s["attribute"]) + 1:].rsplit(":", 1)[0] for k in n}),
                                 "values": len(n), "model": s["model"],
                                 "dispersion": s["dispersion"], "source": s["source"]}
        return out


class Progress:
    """A progress line for a long per-item loop: the count done, the elapsed time and an ETA from the mean time per
    item, at most once per `every_s` seconds and at the last item. It only logs: no output depends on it."""

    def __init__(self, log, label: str, n: int, every_s: float = PROGRESS_EVERY_S, clock=time.monotonic):
        self.log, self.label, self.n, self.every, self.clock = log, label, int(n), float(every_s), clock
        self.t0 = self.last = clock()
        self.done = 0

    def step(self):
        self.done += 1
        if self.log is None:
            return
        now = self.clock()
        if self.done < self.n and now - self.last < self.every:
            return
        self.last = now
        el = now - self.t0
        msg = f"{self.label}: {self.done}/{self.n} done, {el / 60:.1f} min elapsed"
        if self.done < self.n:
            msg += f", ETA {el / self.done * (self.n - self.done) / 60:.1f} min"
        self.log(msg)


def affordability(costs, budget: float, rule: dict | None = None, level: float = 0.95) -> dict:
    """The fixed plan's cost in each draw against the allocatable budget (F6 S1-1). A cost above budget * (1 + 1e-9), the
    solver's own tolerance, counts as over budget. With a rule (`uncertainty.affordability`), rule_met says whether the share of
    draws over budget is at most rule.max_share_over_budget; without one, rule_met is None (reported, not judged).
    No draws, or a non-finite cost, fails closed."""
    c = np.asarray(costs, float)
    if c.size == 0 or not np.all(np.isfinite(c)):
        raise ValueError("affordability: the fixed plan's cost needs one finite value per draw")
    b = float(budget)
    over = int(np.sum(c > b * (1 + 1e-9)))       # the solver's relative budget tolerance (toolkit/alloc BUDGET_RTOL)
    share = over / c.size
    met = None if not rule else bool(share <= float(rule["max_share_over_budget"]) + 1e-12)
    return {"budget_allocatable": b, "draws": int(c.size), "draws_over_budget": over, "share_over_budget": share,
            "fixed_plan_cost": q(c, level), "per_draw_cost": [float(x) for x in c],
            "rule": dict(rule) if rule else None, "rule_met": met}


RESERVE_TOL, RESERVE_FLOOR, RESERVE_GRID, RESERVE_MAX_SOLVES = 0.005, 0.5, 10, 20


def _over(c, budget: float) -> bool:
    return c > float(budget) * (1 + 1e-9)       # affordability()'s tolerance: the solver's relative one


def reserve_search(draw_costs, solve, budget: float, max_share: float, tol: float = RESERVE_TOL,
                   floor: float = RESERVE_FLOOR, max_solves: int = RESERVE_MAX_SOLVES, grid: int = RESERVE_GRID) -> dict:
    """F6 S1-1b: a plan solved within a budget reserve that meets the affordability rule: its cost exceeds the full
    `budget` in at most `max_share` of the draws. `draw_costs` holds one cost table {unit: {package: cost}} per
    uncertainty draw, so a candidate plan's cost in a draw is a sum; `solve(b)` returns the optimum {choice, objective}
    at budget b (RuntimeError "optimizer status infeasible" counts as not meeting).

    Visits: the unconstrained plan (f = 1); if it fails, a grid of `grid` fractions from `floor` up to 1 (step
    (1 - floor) / grid); then bisection between the highest grid fraction that meets the rule and the grid point above
    it, until that bracket is at most `tol` wide or `max_solves` solves are spent. Every solve is a recorded step.
    Plans at different budgets are not nested, so the share need not fall with f: the result is the BEST VISITED plan
    that meets the rule (highest objective; ties: the larger fraction), not a proven optimum, and "none_visited_meets"
    says only that no visited fraction met the rule, not that none exists. `floor_met` reports the floor candidate
    separately. Returns {feasible, result (unconstrained | best_visited | none_visited_meets), fraction, solution,
    draws_over_budget, share_over_budget, floor_met, converged, steps, ...}. Bad inputs fail closed (ValueError)."""
    tabs = list(draw_costs)
    if not tabs or not all(isinstance(t, dict) and t for t in tabs):
        raise ValueError("reserve search: one cost table per uncertainty draw is required")
    if not all(np.isfinite(float(c)) for t in tabs for row in t.values() for c in row.values()):
        raise ValueError("reserve search: every draw's costs must be finite")
    if not (0 <= float(max_share) < 1):
        raise ValueError("reserve search: max_share_over_budget must be in [0, 1)")
    if not (float(tol) > 0 and 0 < float(floor) <= 1 and int(grid) >= 1 and int(max_solves) >= int(grid) + 2):
        raise ValueError("reserve search: tolerance > 0, floor in (0, 1], a grid of at least 1 and at least grid + 2 "
                         "solves are required")
    nd, steps, sols = len(tabs), [], {}

    def step(f):
        rec = {"fraction": float(f), "budget": float(f) * float(budget)}
        try:
            sol = solve(float(f) * float(budget))
        except RuntimeError as e:
            if str(e).strip() != "optimizer status infeasible":
                raise
            steps.append(dict(rec, objective=None, draws_over_budget=None, share_over_budget=None, meets=False,
                              infeasible=True))
            return False
        ch = sol["choice"]
        over = sum(_over(sum(t[i][ch[i]] for i in t), budget) for t in tabs)
        meets = over / nd <= float(max_share) + 1e-12
        steps.append(dict(rec, objective=float(sol["objective"]), draws_over_budget=int(over),
                          share_over_budget=over / nd, meets=bool(meets)))
        sols[len(steps) - 1] = sol
        return meets

    base = {"draws": nd, "tolerance": float(tol), "floor": float(floor), "grid": int(grid), "max_solves": int(max_solves)}
    if step(1.0):
        s0 = steps[0]
        return dict(base, feasible=True, result="unconstrained", fraction=1.0, solution=sols[0],
                    draws_over_budget=s0["draws_over_budget"], share_over_budget=s0["share_over_budget"],
                    floor_met=None, converged=True, solves=1, steps=steps)
    fl, g = float(floor), int(grid)
    pts = [fl + k * (1.0 - fl) / g for k in range(g)] if fl < 1.0 else [1.0]
    met = [f for f in pts if step(f)]
    floor_met = bool(steps[1]["meets"])
    conv = False
    if met:
        lo = max(met)
        hi = next((f for f in pts if f > lo), 1.0)
        while hi - lo > float(tol) + 1e-12 and len(steps) < int(max_solves):
            mid = (lo + hi) / 2
            if step(mid):
                lo = mid
            else:
                hi = mid
        conv = hi - lo <= float(tol) + 1e-12
    ok_ = [j for j, s in enumerate(steps) if s["meets"]]
    if not ok_:
        return dict(base, feasible=False, result="none_visited_meets", fraction=None, solution=None,
                    draws_over_budget=None, share_over_budget=None, floor_met=floor_met, converged=False,
                    solves=len(steps), steps=steps)
    j = max(ok_, key=lambda k: (steps[k]["objective"], steps[k]["fraction"]))
    return dict(base, feasible=True, result="best_visited", fraction=steps[j]["fraction"], solution=sols[j],
                draws_over_budget=steps[j]["draws_over_budget"], share_over_budget=steps[j]["share_over_budget"],
                floor_met=floor_met, converged=conv, solves=len(steps), steps=steps)


def _acosts():
    from toolkit.alloc import costs
    return costs


def _n_seed(A, draws=None, seed=None):
    sp = A.spec["uncertainty"]
    return int(draws if draws is not None else sp.get("draws", 32)), seed if seed is not None else sp.get("seed", 42)


def _draw_rows(A, D, nd, label="uncertainty draws", log=None):
    """Each draw's sampled values and evaluation, in draw order (lazily: the emulator error is drawn from the shared
    stream inside evaluate). On direct evaluation with compute workers (draw_fanout.config) the engine runs of a wave
    of draws fan out first; every draw is then evaluated in order on its own runs, with the same results."""
    from toolkit.analysis import draw_fanout as DF
    P = Progress(log if log is not None else getattr(A, "log", None), label, nd)
    # unit_rates draws reach the engine through the coordinator's DirectModel only: such draws stay in-process
    kw = DF.config(A) if getattr(D, "urng", None) is None else None
    if kw is not None:
        w = DF.wave_size(A)
        for a in range(0, nd, w):
            wave = []
            for _ in range(a, min(nd, a + w)):
                D.last_clamped = []
                theta, zs, noise, mult, sampled = D.draw()
                if noise is not None:       # direct evaluation draws no emulator error (Drawer refuses it)
                    raise ValueError("draw fan-out: a draw carries an emulator error stream on direct evaluation")
                wave.append((theta, zs, mult, sampled, list(D.last_clamped)))
            runs = DF.prefetch(A, [(x[0], x[1]) for x in wave], kw)
            runs.reverse()
            for theta, zs, mult, sampled, clamped in wave:
                A.model.use_runs(theta, zs, runs.pop())        # each draw's outcomes released once evaluated
                out, ben, cost = A.evaluate(theta, zs, noise=None, cost_mult=mult, sampled=sampled)
                yield {"sampled": sampled, "clamped": clamped, "out": out, "ben": ben, "cost": cost,
                       "events": list(A.last_events), "floors": list(getattr(A, "last_cost_floors", None) or [])}
                P.step()
        return
    for _ in range(nd):
        D.last_clamped = []
        theta, zs, noise, mult, sampled = D.draw()
        clamped = list(D.last_clamped)
        ua = {} if getattr(D, "last_units", None) is None else {"unit_attrs": D.last_units}
        out, ben, cost = A.evaluate(theta, zs, noise=noise, cost_mult=mult, sampled=sampled, **ua)
        yield {"sampled": sampled, "clamped": clamped, "out": out, "ben": ben, "cost": cost,
               "events": list(A.last_events), "floors": list(getattr(A, "last_cost_floors", None) or [])}
        P.step()


def evaluate_draws(A, draws=None, seed=None) -> dict:
    """The uncertainty step's draws evaluated once (F6 S1-1b): the same Drawer, seed and number of draws as run(), so
    run(..., pre=) on the result reads exactly these evaluations."""
    nd, sd = _n_seed(A, draws, seed)
    D = Drawer(A, sd)
    D.prepare(nd)
    return {"draws": nd, "seed": int(sd), "drawer": D, "rows": list(_draw_rows(A, D, nd))}


def _on_draw_takes_sampled(fn) -> bool:
    import inspect
    try:
        ps = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    return any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in ps) or len(ps) >= 4


def run(A, plan, headline_name: str, draws=None, seed=None, pre=None, on_draw=None, on_solve=None) -> dict:
    """`on_draw(out, events, cost[, sampled])`: called with each draw's outcomes, model corrections, cost table, and
    optionally `sampled` when the callback accepts 4 positional args (F6 S2-5, S2-6, and CEA §5.7).
    `on_solve(d, sampled, ben, cost, solution, events)`: called after each draw's re-optimised plan is solved and
    guarded (F6 value of information); it only reads, so the outputs are the same with or without it."""
    sp = A.spec["uncertainty"]
    nd, sd = _n_seed(A, draws, seed)
    scope = A.spec.uncertainty_scope()
    if pre is not None:          # evaluated once beforehand (the reserve search): the same draws, checked
        if pre["draws"] != nd or pre["seed"] != int(sd) or len(pre["rows"]) != nd:
            raise ValueError(f"uncertainty: precomputed draws ({pre['draws']}, seed {pre['seed']}) are not this "
                             f"step's ({nd}, seed {sd})")
        D, rows = pre["drawer"], pre["rows"]
    else:
        D = Drawer(A, sd)
        D.prepare(nd)
        rows = _draw_rows(A, D, nd)
    fixed = {o: [] for o in A.outcomes}
    fixed_cost = []
    opt_vals, same = [], np.zeros(len(A.ids))
    comps_used, outs, samples = set(), [], {}
    corr_any = corr_plan = 0
    clamped_draws = []
    floors = []          # F6 S1-3: each draw's floored per-outcome increments (also from precomputed draws)
    hl_outcome = A.spec["deliverable"]["headline"]["outcome"]
    from toolkit.analysis.figure_data import _package_sums
    plan_sums = []           # each draw's per-package sums of the fixed plan (the figure step reads them)
    on_draw_4 = _on_draw_takes_sampled(on_draw) if on_draw is not None else False
    for d, r in enumerate(rows):
        sampled, out, ben, cost = r["sampled"], r["out"], r["ben"], r["cost"]
        if r["clamped"]:
            clamped_draws.append({"draw": d, "units": len(r["clamped"]), "examples": r["clamped"][:5]})
        corr_any += bool(r["events"])
        if on_draw is not None:
            if on_draw_4:
                on_draw(out, r["events"], cost, sampled)
            else:
                on_draw(out, r["events"], cost)
        floors.append(r["floors"])
        row = {}
        for o in A.outcomes:
            v = sum(out[i]["y"][A.comp][o] - out[i]["y"][plan[i]][o] for i in A.ids)
            fixed[o].append(v)
            row[f"{o}_averted"] = v
        row[headline_name] = row[f"{hl_outcome}_averted"]
        fixed_cost.append(sum(cost[i][plan[i]] for i in A.ids))
        plan_sums.append(_package_sums(A, plan, out, cost))
        from toolkit.analysis.core import auxiliary, best_known
        with auxiliary(A, "uncertainty_draws"):      # 1C-c: a feasible plan within the stated gap is used and counted
            sol = best_known(A, A.solve(ben, cost), plan, ben, cost)      # never below a feasible point plan
        A.guard(f"uncertainty draw {d}", [plan, sol["choice"]], r["events"])
        if on_solve is not None:
            on_solve(d, sampled, ben, cost, sol, r["events"])
        opt_vals.append(sol["objective"])
        row["reoptimised_objective"] = sol["objective"]
        same += np.array([sol["choice"][i] == plan[i] for i in A.ids])
        for i in A.ids:
            comps_used |= set(A.pk[sol["choice"][i]])
            row[f"package:{i}"] = sol["choice"][i]
        outs.append(row)
        for k, v in sampled.items():
            samples.setdefault(k, []).append(v)
    lvl = scope["level"]
    if clamped_draws:
        # Codex r2 P1-4: where a draw's calibration clamps, that draw does not carry the full calibration uncertainty;
        # the interval says so instead of claiming unrestricted calibration coverage.
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import render_claims
        meta = dict(scope["interval"])
        meta["covers"] = ["calibration_partly_clamped" if k == "calibration" else k for k in meta["covers"]]
        scope = dict(scope, interval=meta, covers=meta["covers"], label=render_claims.interval_label(meta))
    prov = None
    if A.method == "posterior" and D.post_draws is not None:
        # v2d §3.3: re-read the canonical sample; every draw's applied values must equal its row (fail closed)
        from toolkit.analysis import calibration as C
        smp = C.sample_of(A.lik)
        found = C.check_provenance(smp, D.records)
        prov = {"sample_id": smp["sample_id"], "draws": len(D.records), "rows": [r["row"] for r in D.records],
                "applied": D.records, "findings": found, "alarms": C.spread_alarm(smp, D.records)}
        if found:
            raise ValueError(f"uncertainty: {len(found)} draws are not rows of the canonical posterior sample "
                             f"(e.g. {found[0]})")
        pd = list(A.lik.get("prior_dominated") or [])
        if pd:
            sys.path.insert(0, os.path.join(ROOT, "scripts"))
            import render_claims
            meta = dict(scope["interval"])
            meta["prior_dominated"] = sorted(set(meta.get("prior_dominated") or []) | set(pd))
            scope = dict(scope, interval=meta, label=render_claims.interval_label(meta))
    if D.jc_draws is not None:
        # the joint calibration's draws: every applied value must be a row of its region sample (fail closed)
        from toolkit.analysis import calibration as C
        smp = A.joint["sample"]
        found = C.check_provenance(smp, D.jc_records)
        if found:
            raise ValueError(f"uncertainty: {len(found)} draws are not rows of the joint calibration's region sample "
                             f"(e.g. {found[0]})")
        prov = dict(prov or {}, joint_calibration={"sample_id": smp["sample_id"], "draws": len(D.jc_records),
                                                   "rows": [r["row"] for r in D.jc_records],
                                                   "alarms": C.spread_alarm(smp, D.jc_records)})
    from toolkit.spec import priors as PRI
    plan_ = PRI.plan(A.spec)
    plan_["emulator_error"] = (getattr(A.model, "accuracy", {}) or {}).get("error_model") if "emulator" in D.src else None
    data = {"draws": nd, "seed": int(sp.get("seed", 42)) if seed is None else int(seed),
            "covers": scope["covers"], "excludes": scope["excludes"], "label": scope["label"],
            "fixed_plan": {o: q(v, lvl) for o, v in fixed.items()},
            "reoptimised_objective": q(opt_vals, lvl),
            "units_same_package_share_median": float(np.median(same / max(nd, 1))) if nd else None,
            "units_same_package_in_90pct_of_draws": int(np.sum(same / max(nd, 1) >= 0.9)) if nd else None,
            "components_in_any_draw_plan": sorted(comps_used), "interval": scope["interval"],
            "calibration_clamped": {"draws": len(clamped_draws), "max_units": max((c["units"] for c in clamped_draws),
                                                                                  default=0),
                                    "per_draw": clamped_draws},
            "model_corrections": {"draws_with_corrections": corr_any, "draws_with_corrections_on_plan": 0},
            "headline": {"name": headline_name, "outcome": hl_outcome}, "draw_plan": plan_,
            "affordability": affordability(fixed_cost, A.budget, sp.get("affordability"), lvl) if nd else None,
            "cost_floors": _acosts().draw_floor_summary(floors),
            "prior_dominated": sorted(set(plan_["prior_dominated"]) | set((A.lik or {}).get("prior_dominated") or []))}
    if "emulator" in D.src and getattr(A.model, "error_floors", None) is not None:   # F6 S2-4
        data["emulator_error_floors"] = {"note": "{cost output: {package: n}}, unit values floored at zero after the "
                                                 "drawn emulator error, over every evaluation with that error",
                                         "counts": {k: dict(v) for k, v in A.model.error_floors.items()}}
    if "emulator" in D.src and getattr(A.model, "error_clips", None) is not None:    # 1C-b 2a
        data["emulator_error_clips"] = {"note": "{outcome: {package: n}}, unit reductions clipped to the emulator's "
                                                "bounds after the drawn emulator error (the mean was inside them), "
                                                "over every evaluation with that error",
                                        "counts": {k: dict(v) for k, v in A.model.error_clips.items()}}
    if getattr(A, "plan_budget", None) is not None:      # F6 S1-1b: the draws re-optimised within the reserve
        data["budget_planned"] = float(A.plan_budget)
    if prov is not None:
        data["draw_provenance"] = prov
    ne = D.posterior_provenance()
    if ne is not None:
        data["net_effect_posterior"] = ne      # the signed sha256 the manifest records: the bytes the draws used
    flagged = sorted((getattr(A, "flagged", None) or {}).values(), key=lambda f: str(f["unit"]))
    if flagged:        # calibration.joint.unmatched: flag: these units' values carry no calibrated match
        data["low_confidence_units"] = flagged
    from toolkit.analysis.core import q as _q     # hb1 fix 11 (L): one estimator for this report and uncertainty.yaml
    agg = summarise_outputs(outs, samples, nd, level=scope["level"], summary=_q)   # signed level (Codex P1-11)
    report = {"producer": PRODUCER, "produced_by": "harness", "n_draws": nd, "n_draws_requested": nd,
              "n_successful": nd - len(agg["errors"]), "n_failed": len(agg["errors"]), "seed": data["seed"],
              "interval_label": scope["label"], "interval": scope["interval"], "covers": scope["covers"],
              "excludes": scope["excludes"],
              "degenerate_outputs": agg["degenerate_outputs"],
              "sampler_varied_parameters": len(agg["varied_params"]),
              "scalar_outputs": agg["scalar_outputs"], "categorical_outputs": agg["categorical_outputs"],
              "parameter_samples": agg["parameter_samples"], "backend": "local",
              "sources_drawn": [x["kind"] for x in plan_["sources"] if x["status"] == "drawn"],
              "sources_not_drawn": [{k: x[k] for k in ("kind", "status", "reason") if k in x} for x in plan_["sources"]
                                    if x["status"] != "drawn"],
              "parameters_drawn": [{k: x.get(k) for k in ("name", "source_kind", "dist", "range", "source")}
                                   for x in plan_["drawn"]],
              "parameters_fixed": [{k: x.get(k) for k in ("name", "kind", "value", "reason")} for x in plan_["fixed"]
                                   if x["status"] == "fixed"],
              "not_exposed": plan_["not_exposed"], "emulator_error": plan_["emulator_error"],
              "affordability": data["affordability"]}
    if flagged:
        report["low_confidence_units"] = [{k: f.get(k) for k in ("unit", "region", "status", "reason", "implausibility")}
                                          for f in flagged]
        report["low_confidence_note"] = (f"{len(flagged)} units did not match the joint calibration and proceed "
                                         "flagged (calibration.joint.unmatched: flag); their outcomes rest on an "
                                         "unmatched calibration")
    if D.ne is not None:        # f6-posterior-ranges: these columns' ranges came from the posterior, with its sha256
        report["net_effect_posterior_ranges"] = posterior_range_record(D.ne)
    if callable(getattr(D, "unit_values", None)):     # hb3 fix 5: the per-unit values the draws varied, by route
        data["unit_values_drawn"], report["unit_values_drawn"] = D.unit_values(), D.unit_values()
        report["sampler_varied_unit_values"] = data["unit_values_drawn"]["n_varied"]
    return {"data": data, "report": report,
            "plan_draws": {"draws": nd, "seed": data["seed"], "primary": A.primary, "plan": {i: plan[i] for i in A.ids},
                           "primary_averted": list(fixed[A.primary]), "per_draw": plan_sums}}


def scalar_report(outputs, values: dict, level: float, label: str, n_draws: int, method: str, **extra) -> dict:
    """uncertainty_report.yaml for a deliverable whose headline outputs are scalars (design note v2d §2.3), in the
    shape the allocation path writes and every reader of that file expects: `scalar_outputs: {name: {mean, median,
    sd, ci_low, ci_high, n}}`, one entry per declared output, in the declared order. `outputs` are the deliverable's
    declared output entries ({name, ...}); `values` maps each name to its draws. A declared output without finite draws
    fails closed (ValueError): the report never omits a headline."""
    a = (1.0 - float(level)) / 2.0
    so = {}
    for o in outputs:
        n = o["name"] if isinstance(o, dict) else str(o)
        v = np.asarray(values.get(n, []), float)
        if v.size == 0 or not np.all(np.isfinite(v)):
            raise ValueError(f"uncertainty report: output {n} has no draws or non-finite draws")
        so[n] = {"mean": float(np.mean(v)), "median": float(np.median(v)), "sd": float(np.std(v, ddof=1))
                 if v.size > 1 else 0.0, "ci_low": float(np.quantile(v, a)), "ci_high": float(np.quantile(v, 1 - a)),
                 "n": int(v.size)}
        for k in ("metric", "window", "scale"):
            if isinstance(o, dict) and o.get(k) is not None:
                so[n][k] = o[k]
    return dict({"producer": PRODUCER, "produced_by": "harness", "n_draws": int(n_draws),
                 "n_successful": int(n_draws), "n_failed": 0, "interval_label": label, "level": float(level),
                 "method": method, "backend": "local", "scalar_outputs": so}, **extra)


def write_report(run_dir, report) -> str:
    p = os.path.join(run_dir, "uncertainty_report.yaml")
    from toolkit.analysis.artifacts import plain
    from toolkit.analysis.artifacts import _safe_io
    _safe_io().write_nofollow(run_dir, "uncertainty_report.yaml", yaml.safe_dump(plain(report), sort_keys=False).encode(),
                              atomic=True)
    return p


def _self_test() -> int:
    """The shared scalar writer (scalar_report); the allocation path's run() is exercised by run.py --self-test."""
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    # hb1 fix 11 (L, Codex r1 of #345): the one estimator both uncertainty files use is finite and accurate at the
    # extremes, and gives numpy's values on ordinary draws
    import math
    from toolkit.analysis.core import q as _q
    big = summarise_outputs([{"y": 9e307}, {"y": 1e308}], {}, 2, summary=_q)["scalar_outputs"]["y"]
    ok(all(math.isfinite(big[k]) for k in ("mean", "median")) and abs(big["mean"] / 9.5e307 - 1) < 1e-12
       and abs(big["median"] / 9.5e307 - 1) < 1e-12 and not big.get("degenerate"),
       f"UQE1 draws near the largest float: finite mean and median, not flagged constant ({big})")
    can = _q([1e16, 1.0, -1e16])
    ok(abs(can["mean"] - 1.0 / 3.0) < 1e-15, f"UQE2 a mean with cancelling draws is exact ({can['mean']})")
    s_, m_ = 2.0 ** 1023, sys.float_info.max
    try:
        e4 = [_q([s_, s_, -s_, -0.75 * s_, -0.25 * s_])["mean"], _q([s_, -s_, s_, -0.75 * s_, -0.25 * s_])["mean"],
              _q([m_] * 3)["mean"]]
    except OverflowError as e:
        e4 = [f"OverflowError: {e}"]
    ok(e4 == [0.0, 0.0, m_], f"UQE4 the overflow fallback of the mean is exact and order-free (Codex r2) ({e4})")
    try:
        e5 = _q([m_] * 4, level=0.8)
        e5 = [e5[k] for k in ("ci_low", "median", "ci_high")]
    except Exception as e:  # noqa: BLE001
        e5 = [f"{type(e).__name__}"]
    try:
        e6 = _q(np.array([[1.0], [3.0]]))
        e6 = (e6["mean"], e6["median"])
    except Exception as e:  # noqa: BLE001
        e6 = f"{type(e).__name__}"
    ok(e5 == [m_, m_, m_] and e6 == (2.0, 2.0),
       f"UQE5 identical draws at the largest float give equal quantiles; a column vector is read flat (Codex r2) "
       f"({e5}, {e6})")
    e7 = [_q([s_, s_, -s_, -s_, 0.1, 0.5])["mean"], _q([s_, -s_, s_, -s_, 0.1, 0.5])["mean"]]
    try:
        e8 = (_q(np.empty((2, 0))), str(_q([[1.0, np.nan], [np.nan, np.nan]])))
    except ValueError as e:
        e8 = (None, str(e))
    except Exception as e:  # noqa: BLE001
        e8 = (f"{type(e).__name__}", "")
    ok(e7[0] == e7[1] == 0.1 and e8[0] is None and "3 of 4 values" in e8[1],
       f"UQE6 the mean is the correctly rounded mean whatever the order; draws are counted flat (Codex r3) ({e7}, {e8})")
    v_ = np.random.default_rng(3).normal(5.0, 2.0, 400)
    q_ = _q(v_, 0.9)
    ok(q_["median"] == float(np.median(v_)) and q_["ci_low"] == float(np.percentile(v_, 100 * (1 - 0.9) / 2))
       and abs(q_["mean"] - float(v_.mean())) <= 1e-12 * abs(float(v_.mean())),
       f"UQE3 on ordinary draws the median and bounds are numpy's, the mean equal to the last digits ({q_})")
    rng = np.random.default_rng(1)
    outs = [{"name": "b", "metric": "m2", "window": "w", "scale": "ratio"}, {"name": "a", "metric": "m1"}]
    vals = {"a": rng.normal(1.0, 0.1, 4000), "b": rng.normal(5.0, 1.0, 4000)}
    try:
        rep = scalar_report(outs, vals, 0.9, "90% UI over calibration", 4000, "posterior")
    except Exception as e:  # noqa: BLE001 -- a missing writer is a failing case
        rep = {"error": f"{type(e).__name__}: {e}"}
    so = rep.get("scalar_outputs") or {}
    ok(list(so) == ["b", "a"] and all(set(v) >= {"mean", "median", "sd", "ci_low", "ci_high", "n"} for v in so.values()),
       f"U1 scalar_report: one entry per declared output, in the declared order, with mean, median, sd and interval "
       f"({rep.get('error') or list(so)})")
    if so:
        lo, hi = np.quantile(vals["a"], [0.05, 0.95])
        ok(abs(so["a"]["ci_low"] - lo) < 1e-12 and abs(so["a"]["ci_high"] - hi) < 1e-12 and so["b"]["metric"] == "m2",
           "U2 the interval is the declared level's central interval; the output's metric, window and scale are kept")
        ok(rep["interval_label"] == "90% UI over calibration" and rep["n_draws"] == 4000,
           "U3 the report carries the label and the number of draws")
        for label, v in (("no draws", {"a": [], "b": vals["b"]}),
                         ("a non-finite draw", {"a": [1.0, float("nan")], "b": vals["b"]})):
            try:
                scalar_report(outs, v, 0.9, "x", 2, "posterior")
                ok(False, f"U4 an output with {label} fails closed")
            except ValueError:
                ok(True, f"U4 an output with {label} fails closed")
        with tempfile.TemporaryDirectory() as d:
            p = write_report(os.path.realpath(d), rep)
            ok((yaml.safe_load(open(p)) or {}).get("scalar_outputs", {}).get("a", {}).get("n") == 4000,
               "U5 write_report publishes uncertainty_report.yaml, which reads back")
    # F6 S1-1: the fixed plan's cost across the draws against the allocatable budget
    aff = globals().get("affordability")
    ok(callable(aff), "U6 affordability() exists")
    if callable(aff):
        r = aff([90.0, 110.0, 120.0, 95.0], 100.0, None, 0.9)
        ok(r["draws"] == 4 and r["draws_over_budget"] == 2 and abs(r["share_over_budget"] - 0.5) < 1e-12
           and r["rule_met"] is None and r["budget_allocatable"] == 100.0,
           f"U7 two of four draws over budget, share 0.5; no rule declared: rule_met is None ({r})")
        fc = r.get("fixed_plan_cost") or {}
        ok(fc.get("level") == 0.9 and fc.get("ci_low") <= fc.get("median") <= fc.get("ci_high"),
           "U8 the fixed plan's cost quantiles at the declared level")
        ok(aff([90.0, 110.0, 120.0, 95.0], 100.0, {"max_share_over_budget": 0.1}, 0.95)["rule_met"] is False,
           "U9 share 0.5 against a rule of at most 0.1: the rule is not met")
        ok(aff([90.0, 110.0, 120.0, 95.0], 100.0, {"max_share_over_budget": 0.5}, 0.95)["rule_met"] is True,
           "U10 share 0.5 against a rule of at most 0.5: met")
        ok(aff([100.0 * (1 + 1e-12)], 100.0, None, 0.95)["draws_over_budget"] == 0,
           "U11 a cost equal to the budget up to rounding is not over budget")
        ok(aff([2e-10], 1e-10, None, 0.95)["draws_over_budget"] == 1,
           "U11b the tolerance is relative to the budget, as the solver's (no absolute floor): 2e-10 is over 1e-10")
        for label, v in (("no draws", []), ("a non-finite cost", [90.0, float("nan")])):
            try:
                aff(v, 100.0, None, 0.95)
                ok(False, f"U12 {label} fails closed")
            except ValueError:
                ok(True, f"U12 {label} fails closed")
    _self_test_reserve(ok)
    try:
        _self_test_progress(ok)
    except Exception as e:  # noqa: BLE001 -- a missing feature is a failing case
        ok(False, f"UP1-UP3 the per-draw progress line ({type(e).__name__}: {e})")
    try:
        _self_test_exception(ok)
    except Exception as e:  # noqa: BLE001 -- a missing feature is a failing case
        ok(False, f"UE1 the posterior exception ({type(e).__name__}: {e})")
    try:
        _self_test_posterior(ok)
    except Exception as e:  # noqa: BLE001 -- a missing feature is a failing case
        ok(False, f"NP1-NP4 the net-effect posterior in the draws ({type(e).__name__}: {e})")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def _self_test_exception(ok):
    """UE1 (owner, 2026-11-20): uncertainty.net_effect_posterior.exception is validated with the posterior field, is
    passed to the loader, recorded with the posterior's ranges (manifest and report) and shown as a HIGH owner-signed
    departure on the sign-off sheet."""
    class Sp:
        def __init__(s, raw):
            s.raw = raw

        def resolve(s, p):
            return p
    exc = {"failed_points": [{"point": {"a": 0.1}, "targets": ["T1"]}], "rule": "the 14 Nov acceptance rule",
           "evidence": "13 of 14 verified points pass", "reason": "a narrow miss", "draws": "keep"}
    got = {}
    for label, e in (("good", exc), ("no reason", {k: v for k, v in exc.items() if k != "reason"}),
                     ("bad draws", dict(exc, draws="maybe")), ("no points", dict(exc, failed_points=[])),
                     ("a target not a string", dict(exc, failed_points=[{"point": {"a": 0.1}, "targets": [3]}]))):
        try:
            got[label] = posterior_exception(Sp({"uncertainty": {"net_effect_posterior": {
                "path": "p.yaml", "sha256": "0" * 64, "exception": e}}}))
        except ValueError as ex:
            got[label] = str(ex)
    lines = exception_sheet_lines({"uncertainty": {"net_effect_posterior": {"path": "p.yaml", "sha256": "ab" * 32,
                                                                             "exception": exc}}})
    rec = posterior_range_record({"sha256": "s", "margin": 0.0, "ranges": {"a": (0.0, 1.0)}, "exception": exc})
    ok(got["good"] == exc and all(isinstance(got[k], str) for k in got if k != "good")
       and any("**HIGH**" in x and "T1" in x and "a narrow miss" in x for x in lines)
       and exception_sheet_lines({"uncertainty": {}}) == [] and rec.get("exception") == exc,
       f"UE1 the posterior exception: validated ({sorted(k for k in got if isinstance(got[k], str))} refused), on the "
       f"sheet as HIGH, in the range record ({lines[:2]})")


def _self_test_posterior(ok):
    """NP1-NP4: uncertainty.net_effect_posterior draws whole rows of the net-effect fit's verified joint sample."""
    import tempfile
    import types
    from toolkit.engines.malaria.engine import MalariaEngine
    from toolkit.engines import effect_fit as EF
    from toolkit.spec import priors as PRI
    e = MalariaEngine()
    card = PRI.load_card(e)["priors"]

    class Sp:
        def __init__(s, raw, base):
            s.raw, s.base = raw, base

        def __getitem__(s, k):
            return s.raw[k]

        def resolve(s, p):
            return p if os.path.isabs(p) else os.path.join(s.base, p)

    import hashlib

    def signed(d, name, digest=None):       # the spec field: the path and the owner-signed sha256 of its bytes
        p = os.path.join(d, name)
        return {"path": name, "sha256": digest or (hashlib.sha256(open(p, "rb").read()).hexdigest()
                                                   if os.path.exists(p) else "0" * 64)}

    def fake(d, post=None, sources=("engine_parameters",), cal=()):
        post = signed(d, post) if isinstance(post, str) else post
        u = dict({"sources": [{"kind": k} for k in sources]}, **({"net_effect_posterior": post} if post else {}))
        A = types.SimpleNamespace(spec=Sp({"uncertainty": u, "calibration": {"method": "none", "parameters": list(cal)}},
                                          d), eng=e, region=None, method="none", joint=None, lik=None, model=None,
                                  ids=["u1"], anchor=None, fr={"cfp_kill": (0.3, 0.7, 0.485)},
                                  pr={k: card[k] for k in ("net_kill_shift", "use_rate")},
                                  theta0={"net_kill_shift": 0.0, "use_rate": 0.871, "cfp_kill": 0.485})
        A.calibrate = lambda theta, shifts=None, ids=None: {"u1": (1.0, False)}
        return A
    x = [float(v) for v in np.random.default_rng(3).uniform(0, 1, 30)]
    rows = [{"net_kill_shift": round(-0.5 + v, 6), "cfp_kill": round(0.35 + 0.3 * v, 6)} for v in x]
    box = {"net_kill_shift": [-0.618, 0.66], "cfp_kill": [0.0, 0.8]}
    with tempfile.TemporaryDirectory() as d:
        EF._fixture_placeholder(os.path.join(d, "net_effect_fit.yaml"), e, rows, box)
        # NP1: recorded before this change (branch f6-netfit-adopt): the first draw at seed 9 without the field
        th0 = {"net_kill_shift": 0.11657722739764786, "use_rate": 0.87768660115964, "cfp_kill": 0.6480996815880338}
        D0, D1 = Drawer(fake(d), 9), Drawer(fake(d, "net_effect_fit.yaml"), 9)
        t0, t1 = [D0.draw()[0] for _ in range(60)], [D1.draw()[0] for _ in range(60)]
        pairs = {(r["net_kill_shift"], r["cfp_kill"]) for r in rows}
        c1 = np.corrcoef([t["net_kill_shift"] for t in t1], [t["cfp_kill"] for t in t1])[0, 1]
        c0 = np.corrcoef([t["net_kill_shift"] for t in t0], [t["cfp_kill"] for t in t0])[0, 1]
        ok(D0.ne is None and th0 == t0[0] and all((t["net_kill_shift"], t["cfp_kill"]) in pairs for t in t1)
           and [t["use_rate"] for t in t0] == [t["use_rate"] for t in t1] and c1 > 0.999 and abs(c0) < 0.5,
           f"NP1 with uncertainty.net_effect_posterior each draw takes one whole row of the fit's joint sample (corr "
           f"{c1:.3f}; independent draws {c0:.3f}); a parameter without a sample (use_rate) is drawn from its prior "
           "exactly as without it, and without the field the draws are unchanged")
        pv = D1.posterior_provenance()
        sig = signed(d, "net_effect_fit.yaml")
        ok(pv["sha256"] == sig["sha256"] and len(pv["rows"]) == 60 and pv["names"] == list(box)
           and posterior_input(fake(d).spec) is None,
           "NP2 the uncertainty report records the signed sha256, the names and the row of every draw")
        # Codex r1 P2-4: the file is read once; the manifest, the engine artifact and the drawer use those bytes
        A5 = fake(d, sig)
        i1 = posterior_input(A5.spec)                   # run.py's read, before the manifest is written
        EF._fixture_placeholder(os.path.join(d, "net_effect_fit.yaml"), e, rows[1:], box)      # the file is replaced after
        D5 = Drawer(A5, 9)
        try:
            posterior_input(fake(d, sig).spec)          # a fresh read of the replaced file under the signed digest
            fresh = "accepted"
        except EF.PosteriorRefused as ex:
            fresh = str(ex)
        ok(i1["sha256"] == sig["sha256"] == D5.posterior_provenance()["sha256"] and len(D5.ne["rows"]) == 30
           and "signed sha256" in fresh,
           f"NP3 the sample is read once: the drawer uses the bytes whose signed sha256 the manifest records (30 rows, "
           f"not the replacement's {len(rows) - 1}); a file whose bytes differ from the signed sha256 is refused "
           f"({fresh[:90]})")
        refused = []
        for label, A, why in (("a missing file", fake(d, "absent.yaml"), "unreadable"),
                              ("a calibrated parameter", fake(d, "net_effect_fit.yaml", cal=("cfp_kill",)),
                               "not varied"),
                              ("a fitted parameter without the engine_parameters source",
                               fake(d, "net_effect_fit.yaml", sources=("costs",)), "not varied")):
            try:
                Drawer(A, 9)
            except EF.PosteriorRefused as ex:
                refused += [label] if why in str(ex) else []
        ok(len(refused) == 3, f"NP4 the draws refuse the sample, each for its reason, for {refused} (fail closed)")
        # NP5 (owner, 14 Nov, f6-posterior-ranges): a sample whose hull exceeds the draw ranges (cfp_kill 0.2-0.8 against
        # the fitted 0.3-0.7) loads; its hull sets its columns' draw ranges, the other parameters keep theirs
        wide = [{"net_kill_shift": round(-0.5 + v, 6), "cfp_kill": round(0.2 + 0.6 * v, 6)} for v in x]
        EF._fixture_placeholder(os.path.join(d, "wide.yaml"), e, wide, box)
        hull = {k: (min(r[k] for r in wide), max(r[k] for r in wide)) for k in box}
        D6 = Drawer(fake(d, "wide.yaml"), 9)
        t6 = [D6.draw()[0] for _ in range(40)]
        pv6 = D6.posterior_provenance()
        ok(D6.ranges["cfp_kill"] == hull["cfp_kill"] and D6.ranges["net_kill_shift"] == hull["net_kill_shift"]
           and D6.ranges["use_rate"] == (card["use_rate"].lo, card["use_rate"].hi)
           and all(hull[k][0] <= t[k] <= hull[k][1] for t in t6 for k in hull)
           and pv6["ranges"] == {k: list(v) for k, v in hull.items()} and pv6["range_source"] == "posterior_hull"
           and pv6["sha256"] == signed(d, "wide.yaml")["sha256"] and pv6["margin"] == 0.0,
           f"NP5 a sample whose hull exceeds the fitted range loads; its hull sets its columns' draw ranges "
           f"({ {k: D6.ranges[k] for k in hull} }), recorded with its sha256 in the uncertainty report")
        D7 = Drawer(fake(d, dict(signed(d, "wide.yaml"), margin=0.1)), 9)
        w7 = {k: hull[k][1] - hull[k][0] for k in hull}
        ok(all(abs(D7.ranges[k][0] - max(hull[k][0] - 0.1 * w7[k], card[k].lo if k in card else 0.0)) < 1e-12
               for k in hull) and D7.posterior_provenance()["margin"] == 0.1,
           f"NP6 a stated margin widens the hull (within hard bounds): { {k: D7.ranges[k] for k in hull} }")
        ok(D0.ranges == {"cfp_kill": (0.3, 0.7), "net_kill_shift": (card["net_kill_shift"].lo, card["net_kill_shift"].hi),
                         "use_rate": (card["use_rate"].lo, card["use_rate"].hi)},
           f"NP7 without the field every draw range is the fitted or prior range, as before: {D0.ranges}")
        # Codex r1 P2-1: a log-uniform prior whose posterior hull reaches zero or below is still drawn (and replaced)
        A8 = fake(d, "wide.yaml")
        A8.pr = dict(A8.pr, net_kill_shift=PRI.Prior("net_kill_shift", 0.1, 0.01, 0.66, "loguniform", "s"))
        try:            # the analysis (core) hands the Drawer the priors with_posterior_ranges rewrote
            A8.fr, A8.pr = with_posterior_ranges(net_effect_sample(A8.spec, e, fitted=A8.fr, priors=A8.pr), A8.fr, A8.pr)
            D8 = Drawer(A8, 9)
            t8 = [D8.draw()[0]["net_kill_shift"] for _ in range(5)]
            why8 = "" if all(v in {r["net_kill_shift"] for r in wide} for v in t8) else f"not sample values {t8}"
        except Exception as ex:  # noqa: BLE001
            why8 = f"{type(ex).__name__}: {ex}"
        ok(not why8, f"NP8 a log-uniform prior whose posterior hull crosses zero is drawn without error ({why8 or 'ok'})")
        # Codex r1 P2-3: the cached sample is per engine: another engine's identity is checked again, and refused
        from toolkit.engines.contract import ToyEngine
        A9 = fake(d, "wide.yaml")
        net_effect_sample(A9.spec, e, fitted=A9.fr, priors=A9.pr)
        try:
            net_effect_sample(A9.spec, ToyEngine(), fitted=A9.fr, priors=A9.pr)
            why9 = "accepted"
        except EF.PosteriorRefused as ex:
            why9 = str(ex)
        ok("fitted under engine" in why9, f"NP9 the cached sample does not bypass another engine's check: {why9[:100]}")


def _self_test_progress(ok):
    """hb1 flu R2: 200 direct draws ran 14.6 h with no line between start and end. Long per-draw loops log progress
    with the elapsed time and an ETA, at most once per interval (and at the last item); the log never changes outputs."""
    lines, now = [], [0.0]
    P = Progress(lines.append, "uncertainty draws", 10, every_s=60.0, clock=lambda: now[0])
    for k in range(10):
        now[0] += 25.0           # 25 s per draw
        P.step()
    ok(len(lines) == 4 and lines[-1].startswith("uncertainty draws: 10/10") and "ETA" not in lines[-1],
       f"UP1 a 10-draw loop at 25 s per draw logs at most once a minute and at the end ({lines})")
    ok("ETA 2.9 min" in lines[0] and "3/10" in lines[0],
       f"UP2 each line carries the count done and the ETA from the mean time per item ({lines[:1]})")
    quiet = Progress(None, "x", 3, clock=lambda: 0.0)
    for _ in range(3):
        quiet.step()
    ok(quiet.done == 3, "UP3 without a log the counter still runs and nothing is written")


def _self_test_reserve(ok):
    """F6 S1-1b: the reserve search on synthetic cost tables. Four units, each either nothing (cost 0) or one
    package costing 10 x the draw's multiplier, benefits 4, 3, 2, 1; the exact optimum at budget b funds the
    floor(b / 10) best units. Budget 40; multipliers 0.8 ... 2.0: the unconstrained plan (4 units) is over in 5 of 8
    draws; 3 units are over in 2 of 8 (multipliers 1.5 and 2.0); 2 units in none."""
    rs = globals().get("reserve_search")
    ok(callable(rs), "U13 reserve_search() exists")
    if not callable(rs):
        return
    mults = [0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 2.0]
    ben = {"u1": 4.0, "u2": 3.0, "u3": 2.0, "u4": 1.0}
    tables = [{u: {"n": 0.0, "y": 10.0 * m} for u in ben} for m in mults]
    calls = []

    def solve(b):
        calls.append(b)
        k = int(b / 10.0 + 1e-9)
        top = sorted(ben, key=lambda u: -ben[u])[:k]
        return {"choice": {u: ("y" if u in top else "n") for u in ben}, "objective": sum(ben[u] for u in top)}

    r = rs(tables, solve, 40.0, 0.25, tol=0.005, floor=0.5, max_solves=20, grid=10)
    st = r.get("steps") or []
    ok(r.get("feasible") is True and st and st[0]["fraction"] == 1.0 and st[0]["meets"] is False
       and st[0]["draws_over_budget"] == 5,
       f"U14 the first step is the unconstrained plan (fraction 1), over budget in 5 of 8 draws: {st[:1]}")
    ok([round(x["fraction"], 6) for x in st[1:11]] == [round(0.5 + 0.05 * k, 6) for k in range(10)]
       and r.get("floor_met") is True,
       f"U14b a coarse grid over [floor, 1) is probed before bisecting (Codex S1-1b P2): "
       f"{[round(x['fraction'], 3) for x in st[1:11]]}")
    ok(1.0 - 0.005 <= r.get("fraction", 0) < 1.0 and sum(v == "y" for v in r["solution"]["choice"].values()) == 3
       and r["draws_over_budget"] == 2 and abs(r["share_over_budget"] - 0.25) < 1e-12
       and r.get("result") == "best_visited",
       f"U15 the best visited fraction meeting the rule, bisected to the tolerance above the grid (3 units, 2 of 8 "
       f"over): {r.get('fraction')}, {r.get('draws_over_budget')}, {r.get('result')}")
    ok(len(calls) == len(st) <= 20 and r.get("converged") is True and abs(r["solution"]["objective"] - 9.0) < 1e-12
       and all(x["meets"] == (x["share_over_budget"] <= 0.25) for x in st)
       and all(abs(b - x["fraction"] * 40.0) < 1e-9 for b, x in zip(calls, st)),
       f"U16 every ILP solve is one recorded step at fraction x budget, within the cap ({len(calls)} solves)")
    r1 = rs(tables, solve, 40.0, 0.7, tol=0.005, floor=0.5, max_solves=20, grid=10)
    ok(r1["fraction"] == 1.0 and len(r1["steps"]) == 1 and r1["feasible"] and r1.get("result") == "unconstrained",
       "U17 an unconstrained plan that meets the rule needs no reserve (fraction 1, one solve)")
    r2 = rs(tables, solve, 40.0, 0.0, tol=0.005, floor=0.8, max_solves=20, grid=10)
    ok(r2["feasible"] is False and r2.get("solution") is None and r2.get("result") == "none_visited_meets"
       and r2.get("floor_met") is False and len(r2["steps"]) == 11,
       f"U18 no visited fraction meets the rule (the unconstrained plan and the grid from the floor): nothing "
       f"returned, and the result says only what was visited ({r2.get('result')}, {len(r2['steps'])} solves)")
    r3 = rs(tables, solve, 40.0, 0.25, tol=0.005, floor=0.5, max_solves=12, grid=10)
    ok(len(r3["steps"]) == 12 and r3["converged"] is False and r3["feasible"] and r3["share_over_budget"] <= 0.25,
       "U19 the solve cap stops the bisection unconverged, at a fraction that meets the rule")

    def infeasible_low(b):
        if b < 30:
            raise RuntimeError("optimizer status infeasible")
        return solve(b)
    r4 = rs(tables, infeasible_low, 40.0, 0.25, tol=0.005, floor=0.5, max_solves=20, grid=10)
    ok(r4["feasible"] is True and r4["steps"][1].get("infeasible") is True and r4.get("floor_met") is False
       and r4["fraction"] >= 0.95,
       "U20 the floor candidate failing (the solver proves it infeasible) is not the end: a higher visited fraction "
       "that meets the rule is kept")

    def non_nested(b):          # Codex S1-1b P2: plans that are not nested in the budget
        f = b / 40.0
        k, obj = (1, 1.0) if f < 0.7 else (3, 3.0) if 0.85 <= f < 0.95 else (4, 4.0)
        top = sorted(ben, key=lambda u: -ben[u])[:k]
        return {"choice": {u: ("y" if u in top else "n") for u in ben}, "objective": obj}
    r5 = rs(tables, non_nested, 40.0, 0.25, tol=0.005, floor=0.5, max_solves=20, grid=10)
    ok(r5["feasible"] and abs(r5["solution"]["objective"] - 3.0) < 1e-12 and 0.9 <= r5["fraction"] < 0.95,
       f"U23 a non-nested problem: the grid finds the better plan a plain bisection misses (objective "
       f"{r5['solution']['objective']} at {r5['fraction']})")
    for label, args in (("no draws", ([], solve, 40.0, 0.25)), ("a non-finite cost",
                        ([{u: {"n": 0.0, "y": float("nan")} for u in ben}], solve, 40.0, 0.25)),
                        ("a share outside [0, 1)", (tables, solve, 40.0, 1.0))):
        try:
            rs(*args)
            ok(False, f"U21 {label} fails closed")
        except ValueError:
            ok(True, f"U21 {label} fails closed")
    for kw in ({"tol": 0.0}, {"floor": 0.0}, {"floor": 1.5}, {"max_solves": 1}, {"max_solves": 5, "grid": 10},
               {"grid": 0}):
        try:
            rs(tables, solve, 40.0, 0.25, **kw)
            ok(False, f"U22 search settings {kw} are refused")
        except ValueError:
            ok(True, f"U22 search settings {kw} are refused")



if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
