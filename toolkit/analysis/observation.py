#!/usr/bin/env python3
"""Observation sources: the forward maps of the measurement-bias mechanisms (design note v2d §3.4).

    python3 toolkit/analysis/observation.py --self-test

The quantity of interest is latent (true coverage, immunity or incidence). Each data set is an
observation source that sees it through a sampling frame and a measurement process. A bias is always
a named mechanism with a fixed functional form and a parameter with a prior, never a free multiplier.
This module holds only the closed vocabulary and the forward maps (latent value -> expected observed
value), which the compartmental pack's OB1 checks against hand-computed values. The likelihoods,
priors and the per-source measurement model of a calibration are workstream β (v2d §3.1), not here.

Mechanisms (closed vocabulary; an unknown name fails closed):
  numerator_inflation   reported = value (1 + omega)            over-reporting, doses outside the target
  denominator_error     reported = value / (1 + delta)          target population mis-estimated
  admin_coverage        both of the above: c (1 + omega) / (1 + delta) (can exceed 1)
  assay                 positivity = Se p + (1 - Sp)(1 - p)     assay sensitivity and specificity
  antibody_waning       p exp(-rate years)                      protected people below the assay cut-off
  frame_gap             (1 - phi) p + phi min(rel p, 1)         unit value from the frame and the out-of-frame share
  under_ascertainment   reported = rho value                    a reporting or detection fraction
  recall_bias           k c + (1 - k) min(c (1 + beta), 1)      card share k, recall over-reporting beta
Nothing here names a disease.
"""
from __future__ import annotations

import math
import sys

import numpy as np


def _p(x, what, lo=0.0, hi=1.0):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(float(x)):
        raise ValueError(f"{what}: {x!r} is not a finite number")
    x = float(x)
    if not lo <= x <= hi:
        raise ValueError(f"{what} = {x} outside [{lo}, {hi}]")
    return x


INF = float("inf")


def numerator_inflation(value, omega):
    return _p(value, "value", 0, INF) * (1.0 + _p(omega, "omega", -1, INF))


def denominator_error(value, delta):
    return _p(value, "value", 0, INF) / (1.0 + _p(delta, "delta", -0.999999, INF))


def admin_coverage(coverage, omega, delta):
    return denominator_error(numerator_inflation(_p(coverage, "coverage"), omega), delta)


def assay(p, se, sp):
    p = _p(p, "p")
    return _p(se, "se") * p + (1.0 - _p(sp, "sp")) * (1.0 - p)


def antibody_waning(p, rate, years):
    return _p(p, "p") * math.exp(-_p(rate, "rate", 0, INF) * _p(years, "years", 0, INF))


def frame_gap(p, phi, rel):
    p = _p(p, "p")
    return (1.0 - _p(phi, "phi")) * p + phi * min(_p(rel, "rel", 0, INF) * p, 1.0)


def under_ascertainment(value, rho):
    return _p(value, "value", 0, INF) * _p(rho, "rho")


def recall_bias(coverage, card_share, beta):
    c, k = _p(coverage, "coverage"), _p(card_share, "card_share")
    return k * c + (1.0 - k) * min(c * (1.0 + _p(beta, "beta", -1, INF)), 1.0)


MECHANISMS = {"numerator_inflation": numerator_inflation, "denominator_error": denominator_error,
              "admin_coverage": admin_coverage, "assay": assay, "antibody_waning": antibody_waning,
              "frame_gap": frame_gap, "under_ascertainment": under_ascertainment, "recall_bias": recall_bias}


def forward(mechanism: str, **args) -> float:
    """One mechanism's forward map; an unknown mechanism fails closed."""
    if mechanism not in MECHANISMS:
        raise ValueError(f"bias mechanism {mechanism!r} not in the closed vocabulary {sorted(MECHANISMS)}")
    return float(MECHANISMS[mechanism](**args))


def band_positivity(immune, sizes, se: float = 1.0, sp: float = 1.0, waning=None) -> float:
    """Expected positivity of a survey band that mixes several cohorts: each cohort's immune share (after
    waning, if given as (rate, years) per cohort) through the assay, weighted by cohort size."""
    imm = np.asarray(immune, float)
    w = np.asarray(sizes, float)
    if imm.shape != w.shape or not len(w) or np.any(w < 0) or w.sum() <= 0:
        raise ValueError("band_positivity: immune shares and cohort sizes must align, sizes >= 0 and not all 0")
    if waning is not None:
        imm = np.asarray([antibody_waning(p, r, y) for p, (r, y) in zip(imm, waning)])
    pos = np.asarray([assay(float(p), se, sp) for p in imm])
    return float(np.dot(pos, w) / w.sum())


# ------------------------------------------------------------------ v2d β: observation likelihoods (§3.1)
LIKELIHOODS = ("poisson", "negbin", "binomial", "betabinomial", "lognormal")


def _arr(x, what):
    a = np.asarray(x, float)
    if not np.all(np.isfinite(a)):
        raise ValueError(f"{what}: non-finite values")
    return a


def _counts(y, what="observed counts"):
    a = _arr(y, what)
    if np.any(a < 0) or np.any(a != np.round(a)):
        raise ValueError(f"{what}: must be non-negative integers")
    return a


def loglik(model: str, y, **kw) -> np.ndarray:
    """Element-wise log-likelihood of observations `y` under one observation model (vectorised, broadcasting):
      poisson       y | mu
      negbin        y | mu, dispersion (the size k: variance mu + mu^2 / k)
      binomial      y | n, p
      betabinomial  y | n, p, design_effect (ICC = (deff - 1)/(n - 1); variance n p (1 - p) deff; deff 1 = binomial)
      lognormal     y | mu, sd (log y ~ N(log mu, sd); reporting noise of a positive report)
    Invalid observations or parameters and unknown models fail closed (ValueError)."""
    from scipy.special import betaln, gammaln, xlogy
    if model not in LIKELIHOODS:
        raise ValueError(f"observation model {model!r} not in {LIKELIHOODS}")
    if model in ("poisson", "negbin"):
        y = _counts(y)
        mu = _arr(kw["mu"], "mu")
        if np.any(mu < 0):
            raise ValueError("mu must be >= 0")
        if model == "poisson":
            return xlogy(y, mu) - mu - gammaln(y + 1)
        k = _arr(kw["dispersion"], "dispersion")
        if np.any(k <= 0):
            raise ValueError("negative-binomial dispersion (size) must be > 0")
        return (gammaln(y + k) - gammaln(k) - gammaln(y + 1) + k * (np.log(k) - np.log(k + mu))
                + xlogy(y, mu) - xlogy(y, k + mu))
    if model in ("binomial", "betabinomial"):
        y = _counts(y)
        n = _counts(kw["n"], "n")
        p = _arr(kw["p"], "p")
        if np.any(y > n) or np.any(p < 0) or np.any(p > 1):
            raise ValueError("binomial: need y <= n and p in [0, 1]")
        lc = gammaln(n + 1) - gammaln(y + 1) - gammaln(n - y + 1)
        deff = _arr(kw.get("design_effect", 1.0), "design_effect") if model == "betabinomial" else np.asarray(1.0)
        if np.any(deff < 1):
            raise ValueError("design_effect must be >= 1")
        if model == "binomial" or np.all(deff == 1.0):
            return lc + xlogy(y, p) + xlogy(n - y, 1 - p)
        rho = (deff - 1.0) / np.maximum(n - 1.0, 1.0)
        if np.any(rho >= 1):
            raise ValueError("design effect too large for the sample size (ICC >= 1)")
        t = 1.0 / rho - 1.0
        pc = np.clip(p, 1e-300, 1 - 1e-16)
        a, b = pc * t, (1 - pc) * t
        return lc + betaln(y + a, n - y + b) - betaln(a, b)
    y = _arr(y, "observed values")
    mu = _arr(kw["mu"], "mu")
    sd = _arr(kw["sd"], "sd")
    if np.any(y <= 0) or np.any(sd <= 0):
        raise ValueError("lognormal: observed values and sd must be > 0")
    with np.errstate(divide="ignore"):
        lmu = np.log(mu)
    return -np.log(y) - np.log(sd) - 0.5 * math.log(2 * math.pi) - 0.5 * ((np.log(y) - lmu) / sd) ** 2


# --------------------------------------------- v2d β: the per-source measurement model (§3.4, owner rule)
# No source is truth; each declares a sampling frame and its measurement process. A bias is a mechanism from this
# closed vocabulary, with a parameter, a prior and a source; never a free multiplier fitted to the residual.
DESIGNS = ("administrative", "cluster_survey", "facility_based", "sentinel")
MEASURES = ("dose_coverage", "seropositivity", "case_notifications")
LATENT = ("coverage", "immunity", "incidence", "attack_rate")
BIAS = {   # mechanism: (what it maps, multiplicative factor or None)
    "numerator_inflation": "reported = value (1 + omega)",
    "denominator_error": "reported = value / (1 + delta)",
    "under_ascertainment": "reported = rho value",
    "recall_bias": "reported = k c + (1 - k) min(c (1 + beta), 1) (card share k declared)",
    "antibody_waning": "p exp(-rate years since vaccination)",
    "assay_sensitivity": "positivity = Se p + (1 - Sp)(1 - p)",
    "assay_specificity": "positivity = Se p + (1 - Sp)(1 - p)",
    "maternal_antibody": "handled by excluding ages below a declared bound (no parameter)",
}
ORDER = ("antibody_waning", "recall_bias", "numerator_inflation", "denominator_error", "under_ascertainment",
         "assay")      # the forward maps compose in this order (the assay acts last, on the true positivity)
NOISE = {"case_notifications": ("negbin", "poisson"), "dose_coverage": ("lognormal",),
         "seropositivity": ("betabinomial", "binomial")}
PRIOR_DISTS = ("uniform", "loguniform", "beta", "triangular")


def _prior_ok(p) -> bool:
    if not isinstance(p, dict) or p.get("dist") not in PRIOR_DISTS:
        return False
    if p["dist"] == "beta":
        return all(isinstance(p.get(k), (int, float)) and p[k] > 0 for k in ("a", "b"))
    r = p.get("range")
    return (isinstance(r, (list, tuple)) and len(r) == 2 and all(isinstance(x, (int, float)) for x in r)
            and r[0] < r[1] and (p["dist"] != "loguniform" or r[0] > 0))


def _none_reason(bias):
    """`bias: {none: <reason>}` -> the reason; `bias: none` -> ''; otherwise None."""
    if bias == "none":
        return ""
    if isinstance(bias, dict) and set(bias) == {"none"}:
        return str(bias["none"] or "").strip()
    return None


def check_sources(sources) -> list:
    """Structural checks of observation-source declarations, fail closed: [(code, message)] with the codes of v2d
    §3.4 (observation_source_undeclared, bias_without_mechanism, frame_gap_unmodelled)."""
    out = []
    if not isinstance(sources, list) or not sources:
        return [("observation_source_undeclared", "observations: declare at least one source")]
    gaps = {}
    for s in sources:
        sid = s.get("id") if isinstance(s, dict) else None
        if not sid or not isinstance(s, dict):
            out.append(("observation_source_undeclared", f"a source without an id: {s!r}"))
            continue
        if s.get("measures") not in MEASURES:
            out.append(("observation_source_undeclared", f"{sid}: measures {s.get('measures')!r} not in {MEASURES}"))
        if (s.get("latent") or {}).get("quantity") not in LATENT:
            out.append(("observation_source_undeclared", f"{sid}: latent quantity not in {LATENT}"))
        fr = s.get("frame")
        if not isinstance(fr, dict) or fr.get("design") not in DESIGNS:
            out.append(("observation_source_undeclared", f"{sid}: a frame with design in {DESIGNS} is required"))
        else:
            for ex in fr.get("excluded") or []:
                okx = (isinstance(ex, dict) and ex.get("who") and ex.get("param")
                       and (isinstance(ex.get("share"), (int, float)) or ex.get("share_column"))
                       and _prior_ok(ex.get("relative_value_prior")) and ex.get("source"))
                if not okx:
                    out.append(("frame_gap_unmodelled", f"{sid}: excluded population {ex!r} needs who, param, share, "
                                                        "relative_value_prior and source"))
                    continue
                key = ex["param"]
                sig = (ex.get("share"), ex.get("share_column"), repr(ex["relative_value_prior"]))
                if key in gaps and gaps[key] != sig:
                    out.append(("frame_gap_unmodelled", f"{sid}: excluded population {key} declared differently by "
                                                        "two sources"))
                gaps[key] = sig
        noise = s.get("noise") or {}
        if noise.get("model") not in NOISE.get(s.get("measures"), ()):
            out.append(("observation_source_undeclared", f"{sid}: noise model {noise.get('model')!r} not in "
                                                         f"{NOISE.get(s.get('measures'), ())}"))
        if "bias" not in s:
            out.append(("observation_source_undeclared", f"{sid}: no bias entry (declare mechanisms, or none with a "
                                                         "reason)"))
            continue
        why = _none_reason(s["bias"])
        if why is not None:
            if not why:
                out.append(("bias_without_mechanism", f"{sid}: `bias: none` needs a stated reason"))
            continue
        if not isinstance(s["bias"], list) or not s["bias"]:
            out.append(("bias_without_mechanism", f"{sid}: bias must be a list of mechanisms or {{none: reason}}"))
            continue
        for b in s["bias"]:
            m = (b or {}).get("mechanism") if isinstance(b, dict) else None
            if m not in BIAS:
                out.append(("bias_without_mechanism", f"{sid}: bias mechanism {m!r} is not in the closed vocabulary "
                                                      f"{sorted(BIAS)} (a free multiplier is refused)"))
                continue
            if b.get("per_unit"):
                out.append(("bias_without_mechanism", f"{sid}: per-unit bias {m} is not supported (a per-unit factor "
                                                      "would be a fit to the residual)"))
                continue
            if m == "maternal_antibody":
                # Codex r1-9: the declared age exclusion is not yet applied to observations, denominators and latent
                # predictions, so accepting it would leave infant positives in the likelihood
                out.append(("bias_without_mechanism", f"{sid}: maternal_antibody (exclude_below_age) is not supported "
                                                      "yet: exclude those ages from the target data instead"))
                continue
            if not b.get("param") or not _prior_ok(b.get("prior")) or not str(b.get("source") or "").strip():
                out.append(("bias_without_mechanism", f"{sid}: bias {m} needs a param, a prior (one of {PRIOR_DISTS}) "
                                                      "and a source"))
            if m == "recall_bias" and not isinstance(b.get("card_share"), (int, float)):
                out.append(("bias_without_mechanism", f"{sid}: recall_bias needs the declared card_share"))
    return out


class Source:
    """One declared observation source: the forward map from the latent quantity to the expected observation, its
    parameters (bias mechanisms and frame-gap relative values) with their priors, and the unit value implied by the
    frame and its excluded populations. Vectorised over arrays of parameter values."""

    def __init__(self, decl: dict, as_truth: bool = False):
        bad = check_sources([decl])
        if bad:
            raise ValueError("; ".join(m for _, m in bad))
        self.decl, self.id, self.as_truth = decl, decl["id"], bool(as_truth)
        self.measures, self.latent = decl["measures"], decl["latent"]["quantity"]
        self.noise = dict(decl.get("noise") or {})
        self.design_effect = (decl.get("frame") or {}).get("design_effect", 1.0)
        reason = _none_reason(decl["bias"])
        self.unbiased_reason = "taken as the truth (negative control)" if as_truth else reason
        self.bias = [] if (as_truth or reason is not None) else [b for b in decl["bias"]
                                                                 if b["mechanism"] != "maternal_antibody"]
        self.excluded = [] if as_truth else list(decl["frame"].get("excluded") or [])

    def params(self) -> list:
        return [b["param"] for b in self.bias] + [e["param"] for e in self.excluded]

    def priors(self) -> dict:
        out = {b["param"]: dict(b["prior"], source=b.get("source", "")) for b in self.bias}
        out.update({e["param"]: dict(e["relative_value_prior"], source=e.get("source", "")) for e in self.excluded})
        return out

    def _p(self, vals, m):
        b = next(x for x in self.bias if x["mechanism"] == m)
        return np.asarray(vals[b["param"]], float), b

    def expected(self, latent, vals: dict, years=None):
        """Expected observed value (a coverage, a positivity or a count rate) for latent values of the frame
        population; `years` are years since vaccination (antibody waning)."""
        v = np.asarray(latent, float)
        have = {b["mechanism"] for b in self.bias}
        for m in ORDER:
            if m == "assay":
                if have & {"assay_sensitivity", "assay_specificity"}:
                    se = self._p(vals, "assay_sensitivity")[0] if "assay_sensitivity" in have else 1.0
                    sp = self._p(vals, "assay_specificity")[0] if "assay_specificity" in have else 1.0
                    v = se * v + (1.0 - sp) * (1.0 - v)
                continue
            if m not in have:
                continue
            x, b = self._p(vals, m)
            if m == "antibody_waning":
                if years is None:
                    raise ValueError(f"{self.id}: antibody_waning needs years since vaccination")
                v = v * np.exp(-x * np.asarray(years, float))
            elif m == "recall_bias":
                k = float(b["card_share"])
                v = k * v + (1.0 - k) * np.minimum(v * (1.0 + x), 1.0)
            elif m == "numerator_inflation":
                v = v * (1.0 + x)
            elif m == "denominator_error":
                v = v / (1.0 + x)
            elif m == "under_ascertainment":
                v = v * x
        return v

    def unit_value(self, frame_value, vals: dict, shares=None):
        """The unit's value from the frame population's value and each excluded population (share phi, relative
        value rel): (1 - phi) p + phi min(rel p, 1), applied in turn."""
        v = np.asarray(frame_value, float)
        for e in self.excluded:
            phi = float((shares or {}).get(e["param"], e.get("share")))
            rel = np.asarray(vals[e["param"]], float)
            v = (1.0 - phi) * v + phi * np.minimum(rel * v, 1.0)
        return v

    MULTIPLICATIVE = ("numerator_inflation", "denominator_error", "under_ascertainment")

    def net_name(self) -> str:
        return f"net_bias:{self.id}"

    def net_multiplier(self, vals: dict):
        """The product of the source's multiplicative bias factors (a derived quantity: what overlapping sources
        identify even when the factors trade off)."""
        out = 1.0
        for b in self.bias:
            x = np.asarray(vals[b["param"]], float)
            if b["mechanism"] == "numerator_inflation":
                out = out * (1.0 + x)
            elif b["mechanism"] == "denominator_error":
                out = out / (1.0 + x)
            elif b["mechanism"] == "under_ascertainment":
                out = out * x
        return out

    def n_multiplicative(self) -> int:
        return sum(b["mechanism"] in self.MULTIPLICATIVE for b in self.bias)


def _self_test_beta(ok) -> None:
    """v2d β: observation likelihoods and the per-source measurement model (§3.1, §3.4)."""
    from scipy import stats

    def safe(fn, msg):
        try:
            fn()
        except Exception as e:      # a missing function is a failing case, not a crash of the whole self-test
            ok(False, f"{msg} ({type(e).__name__}: {e})")

    def ol1():
        y = np.arange(0, 400)
        nb = loglik("negbin", y, mu=30.0, dispersion=2.0)
        ref = stats.nbinom.logpmf(y, 2.0, 2.0 / 32.0)
        ok(np.max(np.abs(nb - ref)) < 1e-10 and abs(np.exp(nb).sum() - 1) < 1e-9,
           "OL1 negative binomial (mean mu, size k) equals scipy's nbinom and sums to 1")
        po = loglik("poisson", y[:80], mu=30.0)
        ok(np.max(np.abs(loglik("negbin", y[:80], mu=30.0, dispersion=1e8) - po)) < 1e-4
           and np.max(np.abs(po - stats.poisson.logpmf(y[:80], 30.0))) < 1e-10,
           "OL2 the negative binomial tends to the Poisson as the size grows")
    safe(ol1, "OL1-2 negative binomial")

    def ol3():
        n, p, deff = 300, 0.7, 2.1
        y = np.arange(0, n + 1)
        bb = loglik("betabinomial", y, n=n, p=p, design_effect=deff)
        rho = (deff - 1) / (n - 1)
        a, b = p * (1 / rho - 1), (1 - p) * (1 / rho - 1)
        ok(np.max(np.abs(bb - stats.betabinom.logpmf(y, n, a, b))) < 1e-9, "OL3 beta-binomial from the design effect "
           "(ICC = (deff - 1)/(n - 1)) equals scipy's betabinom")
        pm = np.exp(bb)
        var = float(np.sum(pm * (y - n * p) ** 2))
        ok(abs(var / (n * p * (1 - p)) - deff) < 1e-6, f"OL4 its variance is the binomial variance x the design "
           f"effect ({var / (n * p * (1 - p)):.6f})")
        ok(np.max(np.abs(loglik("betabinomial", y, n=n, p=p, design_effect=1.0) - stats.binom.logpmf(y, n, p))) < 1e-10,
           "OL5 design effect 1 is the binomial")
        ln = loglik("lognormal", np.array([0.8, 1.1]), mu=np.array([0.75, 1.0]), sd=0.05)
        ok(np.max(np.abs(ln - stats.lognorm.logpdf([0.8, 1.1], 0.05, scale=[0.75, 1.0]))) < 1e-10,
           "OL6 lognormal reporting noise around the expected value")
    safe(ol3, "OL3-6 survey and report likelihoods")

    def ol7():
        bad = 0
        for args in (("negbin", [-1], {"mu": 1.0, "dispersion": 2.0}), ("negbin", [1], {"mu": 1.0, "dispersion": 0.0}),
                     ("betabinomial", [3], {"n": 2, "p": 0.5, "design_effect": 1.5}),
                     ("betabinomial", [1], {"n": 10, "p": 1.5, "design_effect": 1.5}),
                     ("betabinomial", [1], {"n": 10, "p": 0.5, "design_effect": 0.5}),
                     ("binomial", [1.5], {"n": 10, "p": 0.5}), ("gaussian_fudge", [1], {"mu": 1.0})):
            try:
                loglik(args[0], np.asarray(args[1]), **args[2])
            except (ValueError, TypeError):
                bad += 1
        ok(bad == 7, f"OL7 invalid observations, parameters and unknown models fail closed ({bad}/7)")
    safe(ol7, "OL7 fail closed")

    def os1():
        good = toy_sources()
        ok(check_sources(good) == [], f"OS1 a declared administrative source and a survey validate: {check_sources(good)}")
        codes = lambda s: sorted({f[0] for f in check_sources(s)})  # noqa: E731
        s = toy_sources()
        s[0]["bias"].append({"mechanism": "correction_factor", "param": "kappa", "per_unit": True,
                             "prior": {"dist": "uniform", "range": [0.5, 2.0]}, "source": "fitted"})
        ok(codes(s) == ["bias_without_mechanism"], f"OS2 a free (per-unit) multiplier is refused: {codes(s)}")
        s = toy_sources()
        del s[1]["bias"][0]["prior"]
        ok(codes(s) == ["bias_without_mechanism"], f"OS3 a bias mechanism without a prior is refused: {codes(s)}")
        s = toy_sources()
        del s[0]["frame"]
        ok(codes(s) == ["observation_source_undeclared"], f"OS4 a source without a frame is refused: {codes(s)}")
        s = toy_sources()
        s[1]["frame"]["excluded"] = [{"who": "mobile households", "share": 0.06}]
        ok(codes(s) == ["frame_gap_unmodelled"], f"OS5 a frame gap without a relative-value prior: {codes(s)}")
        s = toy_sources()
        s[1]["bias"] = "none"
        ok(codes(s) == ["bias_without_mechanism"], f"OS6 `bias: none` needs a reason: {codes(s)}")
        s[1]["bias"] = {"none": "a perfect assay by construction (synthetic)"}
        ok(codes(s) == [], "OS7 `bias: none` with a stated reason is accepted")
    safe(os1, "OS1-7 source declarations")

    def os8():
        src = [Source(d) for d in toy_sources()]
        vals = {"omega": 0.1, "delta": 0.05, "se": 0.95, "sp": 0.98, "wane": 0.02, "rel_out": 0.7}
        c = np.array([0.8, 0.6])
        adm = src[0].expected(c, vals)
        ok(np.allclose(adm, [admin_coverage(x, 0.1, 0.05) for x in c], rtol=0, atol=1e-15),
           "OS8 the administrative source's expected value is the composed forward maps c (1 + omega)/(1 + delta)")
        imm, yrs = np.array([0.7, 0.5]), np.array([0.0, 3.0])
        pos = src[1].expected(imm, vals, years=yrs)
        ref = [assay(antibody_waning(p, 0.02, y), 0.95, 0.98) for p, y in zip(imm, yrs)]
        ok(np.allclose(pos, ref, rtol=0, atol=1e-15), "OS9 the serosurvey composes waning then the assay")
        ok(sorted(src[0].params()) == ["delta", "omega", "rel_out"] and sorted(src[1].params()) == ["rel_out", "se",
                                                                                                    "sp", "wane"],
           f"OS10 each source lists its bias and frame-gap parameters with their priors: {src[0].params()}, "
           f"{src[1].params()}")
        ok(np.allclose(src[1].unit_value(np.array([0.5]), vals), [frame_gap(0.5, 0.06, 0.7)], rtol=0, atol=1e-15),
           "OS11 the unit value mixes the frame and the out-of-frame share phi")
        ok(src[0].net_multiplier(vals) == (1.1 / 1.05) and "net_bias:admin" == src[0].net_name(),
           "OS12 a source's net multiplicative bias is a derived quantity")
    safe(os8, "OS8-12 measurement model")

    def os13():
        codes = lambda s: sorted({f[0] for f in check_sources(s)})  # noqa: E731
        s = toy_sources()
        s[1]["bias"] = {"none": "a perfect assay (toy)"}
        s[1]["noise"] = {"model": "poisson"}
        ok(codes(s) == ["observation_source_undeclared"], f"OS13 (Codex r1-6) the noise model is checked even for an "
                                                          f"unbiased source: {codes(s)}")
        s = toy_sources()
        s[1]["bias"].append({"mechanism": "maternal_antibody", "handling": "exclude_below_age", "bound_months": 9})
        ok(codes(s) == ["bias_without_mechanism"], f"OS14 (Codex r1-9) maternal_antibody exclusion is refused until "
                                                   f"the age mask is applied: {codes(s)}")
    safe(os13, "OS13-14 noise and maternal antibody")


def toy_sources() -> list:
    """An administrative coverage source and a serosurvey with a shared frame gap (self-tests and docs)."""
    gap = {"who": "out-of-frame households", "share": 0.06, "param": "rel_out",
           "relative_value_prior": {"dist": "uniform", "range": [0.5, 1.0]}, "source": "frame documentation"}
    return [
        {"id": "admin", "measures": "dose_coverage", "latent": {"quantity": "coverage"},
         "frame": {"design": "administrative", "population": "target population as projected", "excluded": [dict(gap)]},
         "bias": [{"mechanism": "numerator_inflation", "param": "omega",
                   "prior": {"dist": "uniform", "range": [0.0, 0.25]}, "source": "review of data-quality audits"},
                  {"mechanism": "denominator_error", "param": "delta",
                   "prior": {"dist": "uniform", "range": [-0.10, 0.25]}, "source": "census projection error"}],
         "noise": {"model": "lognormal", "sd": 0.03, "source": "reporting noise"}},
        {"id": "sero", "measures": "seropositivity", "latent": {"quantity": "immunity"},
         "frame": {"design": "cluster_survey", "design_effect": 2.1, "excluded": [dict(gap)]},
         "bias": [{"mechanism": "assay_sensitivity", "param": "se", "prior": {"dist": "uniform", "range": [0.92, 0.98]},
                   "source": "assay validation"},
                  {"mechanism": "assay_specificity", "param": "sp", "prior": {"dist": "uniform", "range": [0.95, 0.995]},
                   "source": "assay validation"},
                  {"mechanism": "antibody_waning", "param": "wane", "prior": {"dist": "uniform", "range": [0.0, 0.05]},
                   "source": "follow-up studies"}],
         "noise": {"model": "betabinomial"}}]


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    ok(abs(forward("admin_coverage", coverage=0.8, omega=0.1, delta=0.05) - 0.88 / 1.05) < 1e-15,
       "OM1 admin coverage c (1 + omega) / (1 + delta)")
    ok(abs(forward("admin_coverage", coverage=0.95, omega=0.2, delta=0.0) - 1.14) < 1e-15,
       "OM2 administrative coverage can exceed 1")
    ok(abs(forward("assay", p=0.0, se=0.9, sp=0.97) - 0.03) < 1e-15, "OM3 assay: false positives at p = 0")
    for bad in ({"mechanism": "kappa", "value": 1.0}, {"mechanism": "assay", "p": 1.2, "se": 1, "sp": 1},
                {"mechanism": "frame_gap", "p": 0.5, "phi": float("nan"), "rel": 1.0}):
        try:
            forward(**bad)
            ok(False, f"OM4 fails closed: {bad}")
        except (ValueError, TypeError):
            ok(True, f"OM4 fails closed: {bad['mechanism']}")
    ok(abs(band_positivity([0.9, 0.7, 0.5], [100, 200, 300]) - (90 + 140 + 150) / 600) < 1e-15,
       "OM5 band positivity with a perfect assay is the size-weighted mean")
    _self_test_beta(ok)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
