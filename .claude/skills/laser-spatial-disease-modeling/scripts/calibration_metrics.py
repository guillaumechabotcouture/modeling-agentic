#!/usr/bin/env python3
"""
Calibration fitness metrics for spatial SEIR models.

Provides two metrics for comparing simulations to observed data:
    1. CCS Similarity - Logistic curve fit to proportion of zero-incidence
       weeks vs. log10(population)
    2. Wavelet Phase Similarity - Cross-wavelet phase difference scoring
       for traveling wave detection

Usage:
    from calibration_metrics import logistic, fit_mean_var, similarity_metric
    from calibration_metrics import phase_similarity, combined_ranking
"""

import numpy as np
from scipy.optimize import curve_fit


# ---- CCS (Critical Community Size) Similarity ----

def logistic(x, x0, k):
    """Logistic function bounded [0,1], transitioning from 1 to 0."""
    return 1 / (1 + np.exp(k * (x - x0)))


def fit_mean_var(x, y):
    """Fit a logistic curve to proportion-zero-vs-log-population data.

    Parameters:
        x: log10(population) values
        y: Proportion of zero-incidence weeks

    Returns:
        popt: Fitted parameters (x0, k) for the logistic function
    """
    p0 = [np.median(x), 1.0]
    bounds = ([-np.inf, 0.01], [np.inf, 10.0])
    popt, _ = curve_fit(logistic, x, y, p0=p0, bounds=bounds, maxfev=10000)
    return popt


def similarity_metric(mean_data, mean_sim):
    """Compute sum of squared differences between fitted logistic curves.

    Parameters:
        mean_data: Fitted (x0, k) for observed data
        mean_sim: Fitted (x0, k) for simulation

    Returns:
        float: Sum of squared differences on a common grid [2.5, 6.5]
    """
    x_grid = np.linspace(2.5, 6.5, 200)
    data_curve = logistic(x_grid, *mean_data)
    sim_curve = logistic(x_grid, *mean_sim)
    return np.sum((sim_curve - data_curve) ** 2)


# ---- Wavelet Phase Similarity ----

def phase_similarity(y_obs, y_sim, mask):
    """Sum of squared differences in phase (degrees) at valid locations.

    Parameters:
        y_obs: Observed phase differences (radians)
        y_sim: Simulated phase differences (radians)
        mask: Boolean mask for valid comparisons

    Returns:
        float: Sum of squared phase differences in degrees
    """
    return np.sum(
        ((-180 / np.pi) * y_obs[mask] - (-180 / np.pi) * y_sim[mask]) ** 2
    )


# ---- Combined Ranking ----

def combined_ranking(results_df):
    """Rank simulations by combined CCS + wavelet phase similarity.

    Parameters:
        results_df: DataFrame with 'similarity_CCS' and
                    'wavelet_phase_similarity' columns

    Returns:
        best_idx: Index of the best combined simulation
        combined_rank: Series of combined rank scores
    """
    ccs_ranks = results_df['similarity_CCS'].rank(ascending=True)
    phase_ranks = results_df['wavelet_phase_similarity'].rank(ascending=True)
    combined_rank = ccs_ranks + phase_ranks
    best_idx = combined_rank.idxmin()
    return best_idx, combined_rank


# ---- calabaria Loss Bridge ----

def compute_calibration_loss(
    model_weekly_incidence,
    observed_data,
    log_populations,
    observed_ccs_params=None,
    observed_phases=None,
    phase_mask=None,
    ccs_weight=0.6,
    phase_weight=0.4,
):
    """Combine CCS + wavelet metrics into a single scalar loss for calabaria TrialResult.

    Parameters:
        model_weekly_incidence: 2D array (weeks × patches) of simulated weekly incidence
        observed_data: dict with 'ccs_params' and/or 'phases' from observed data
        log_populations: log10(population) per patch
        observed_ccs_params: fitted (x0, k) from observed CCS curve (optional)
        observed_phases: observed phase differences in radians (optional)
        phase_mask: boolean mask for valid phase comparisons (optional)
        ccs_weight: weight for CCS similarity in combined loss (default 0.6)
        phase_weight: weight for phase similarity in combined loss (default 0.4)

    Returns:
        float: combined scalar loss (lower is better), suitable for TrialResult(loss=...)
    """
    loss = 0.0
    total_weight = 0.0

    if observed_ccs_params is not None:
        prop_zero = np.mean(model_weekly_incidence == 0, axis=0)
        try:
            sim_params = fit_mean_var(log_populations, prop_zero)
            ccs_loss = similarity_metric(observed_ccs_params, sim_params)
            loss += ccs_weight * ccs_loss
            total_weight += ccs_weight
        except RuntimeError:
            # curve_fit failed — assign high penalty
            loss += ccs_weight * 1e6
            total_weight += ccs_weight

    if observed_phases is not None and phase_mask is not None:
        loss += phase_weight * phase_similarity(observed_phases, np.zeros_like(observed_phases), phase_mask)
        total_weight += phase_weight

    if total_weight > 0:
        return loss / total_weight
    return float("inf")
