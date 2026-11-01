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
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
