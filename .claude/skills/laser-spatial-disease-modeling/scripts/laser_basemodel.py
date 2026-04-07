#!/usr/bin/env python3
"""
Disease-agnostic BaseModel wrapper for LASER spatial SEIR models.

This template wraps a LASER Model (built using the laser-spatial-disease-modeling
skill workflow, Steps 1-6) inside calabaria's BaseModel for structured calibration,
scenario management, and cloud scaling.

Usage:
    Subclass SpatialSEIRModel or use it directly. Customize PARAMS, CONFIG,
    and component lists to match your specific disease and geography.

    from laser_basemodel import SpatialSEIRModel

    model = SpatialSEIRModel(scenario_gdf, distances, birthrates, pyramid)
    outputs = model.simulate({"beta": 3.5, "gravity_k": 0.01, ...}, seed=42)
    print(outputs["weekly_incidence"].head())
"""

import numpy as np
import polars as pl

from calabaria import BaseModel, model_output, model_scenario, ScenarioSpec
from calabaria.parameters import (
    ParameterSpace,
    ParameterSpec,
    ConfigurationSpace,
    ConfigSpec,
)

import laser.core.distributions as dists
from laser.core.propertyset import PropertySet
from laser.core.migration import gravity, row_normalizer
import laser.core.random
from laser.generic import SEIR, Model
from laser.generic.utils import ValuesMap
from laser.generic.vitaldynamics import BirthsByCBR, MortalityByCDR

try:
    from verification_checks import verify_model_health
except ImportError:
    verify_model_health = None


class SpatialSEIRModel(BaseModel):
    """LASER spatial SEIR model wrapped for calabaria calibration.

    Accepts pre-built scenario data (GeoDataFrame, distance matrix, birthrates,
    age pyramid) and constructs the LASER Model from calibration parameters.
    """

    # --- Calibration parameters (uncertain, Optuna explores these) ---
    PARAMS = ParameterSpace([
        ParameterSpec(
            "beta", lower=2.0, upper=6.0, kind="float",
            doc="Transmission rate (scales force of infection)",
        ),
        ParameterSpec(
            "gravity_k", lower=1e-4, upper=0.1, kind="float",
            doc="Gravity model coupling constant",
        ),
        ParameterSpec(
            "gravity_b", lower=0.1, upper=1.5, kind="float",
            doc="Gravity model destination population exponent",
        ),
        ParameterSpec(
            "gravity_c", lower=0.5, upper=3.0, kind="float",
            doc="Gravity model distance decay exponent",
        ),
        ParameterSpec(
            "seasonal_amplitude", lower=0.0, upper=2.0, kind="float",
            doc="Seasonal forcing amplitude (0 = no seasonality)",
        ),
    ])

    # --- Fixed settings (not calibrated) ---
    CONFIG = ConfigurationSpace([
        ConfigSpec("nticks", default=7300, doc="Simulation duration in days (20 years)"),
        ConfigSpec("burnin_years", default=10, doc="Years to discard before analysis"),
        ConfigSpec(
            "capacity_safety_factor", default=3.0,
            doc="LaserFrame pre-allocation multiplier",
        ),
        ConfigSpec("exp_shape", default=40, doc="Gamma shape for exposed duration"),
        ConfigSpec("exp_scale", default=0.25, doc="Gamma scale for exposed duration"),
        ConfigSpec("inf_mean", default=8, doc="Mean infectious period (days)"),
        ConfigSpec("inf_sigma", default=2, doc="Std dev infectious period (days)"),
    ])

    def __init__(self, scenario_gdf, distances, birthrates, deathrates, pyramid):
        """Initialize with pre-built scenario data.

        Args:
            scenario_gdf: GeoDataFrame with columns [nodeid, name, population,
                          geometry, S, E, I, R]. One row per spatial patch.
            distances: 2D numpy array of pairwise distances (km) between patches.
            birthrates: 1D array of crude birth rates per patch (per-1000/year).
            deathrates: 1D array of crude death rates per patch (per-1000/year).
            pyramid: Age pyramid for BirthsByCBR (from KaplanMeierEstimator or similar).
        """
        super().__init__()
        self.scenario_gdf = scenario_gdf
        self.distances = distances
        self.birthrates = birthrates
        self.deathrates = deathrates
        self.pyramid = pyramid

        # Validate birthrate units (critical — see LASER skill Layer 1)
        assert np.all(birthrates >= 1) and np.all(birthrates <= 60), (
            f"Birthrates must be per-1000/year (typical 10-50), "
            f"got {birthrates.min():.4f}-{birthrates.max():.4f}"
        )

    def build_sim(self, params: dict, config: dict) -> Model:
        """Construct the LASER Model from parameters.

        Args:
            params: Calibration parameters (beta, gravity_k, etc.)
            config: Fixed settings (nticks, exp_shape, etc.)

        Returns:
            Configured LASER Model ready to run.
        """
        nticks = config["nticks"]
        nnodes = len(self.scenario_gdf)

        # Duration distributions
        expdurdist = dists.gamma(shape=config["exp_shape"], scale=config["exp_scale"])
        infdurdist = dists.normal(loc=config["inf_mean"], scale=config["inf_sigma"])

        # Seasonal forcing
        amplitude = params["seasonal_amplitude"]
        if amplitude > 0:
            days = np.arange(365)
            season_365 = 1.0 + amplitude * np.cos(2 * np.pi * days / 365)
            season_365 /= season_365.mean()
            season_tiled = np.tile(season_365, nticks // 365 + 1)[:nticks]
            seasonality = ValuesMap.from_timeseries(season_tiled, nnodes)
        else:
            seasonality = ValuesMap.from_scalar(1.0, nticks, nnodes)

        # PropertySet
        parameters = PropertySet({
            "nticks": nticks,
            "beta": params["beta"],
            "cbr": float(np.mean(self.birthrates)),
            "gravity_k": params["gravity_k"],
            "gravity_b": params["gravity_b"],
            "gravity_c": params["gravity_c"],
            "capacity_safety_factor": config["capacity_safety_factor"],
        })

        # Build Model
        model = Model(self.scenario_gdf, parameters, birthrates=self.birthrates)

        # Gravity migration network
        populations = np.array(self.scenario_gdf.population)
        network = gravity(
            populations, self.distances,
            1, 0, params["gravity_b"], params["gravity_c"],
        )
        avg_export = np.mean(network.sum(axis=1))
        if avg_export > 0:
            network = network / avg_export * params["gravity_k"]
        network = row_normalizer(network, 0.2)
        model.network = network

        # Components (customize this list for your disease)
        model.components = [
            SEIR.Susceptible(model),
            SEIR.Exposed(model, expdurdist, infdurdist),
            SEIR.Infectious(model, infdurdist),
            SEIR.Recovered(model),
            SEIR.Transmission(model, expdurdist, seasonality=seasonality),
            BirthsByCBR(model, birthrates=self.birthrates, pyramid=self.pyramid),
            MortalityByCDR(model, mortalityrates=self.deathrates),
        ]

        return model

    def run_sim(self, state: Model, seed: int) -> None:
        """Execute the simulation.

        Args:
            state: The LASER Model from build_sim.
            seed: Random seed for reproducibility.
        """
        laser.core.random.seed(seed)
        state.run()

        # Post-run verification (non-fatal — prints report but doesn't raise)
        if verify_model_health is not None:
            try:
                verify_model_health(state, raise_on_critical=False)
            except Exception as e:
                print(f"Warning: verification check error: {e}")

    @model_output("weekly_incidence")
    def weekly_incidence(self, state: Model) -> pl.DataFrame:
        """Extract post-burn-in weekly incidence by patch.

        Returns:
            pl.DataFrame with columns [week, patch, cases].
        """
        burnin_days = int(self.CONFIG["burnin_years"].default * 365)
        incidence = state.nodes.newly_infected[burnin_days:, :]

        num_weeks = incidence.shape[0] // 7
        weekly = incidence[: num_weeks * 7, :].reshape(
            num_weeks, 7, incidence.shape[1]
        ).sum(axis=1)

        # Build DataFrame
        rows = []
        for w in range(num_weeks):
            for p in range(weekly.shape[1]):
                rows.append({"week": w, "patch": p, "cases": int(weekly[w, p])})

        return pl.DataFrame(rows)

    @model_output("compartments")
    def compartments(self, state: Model) -> pl.DataFrame:
        """Extract S/E/I/R time series (daily, all patches).

        Returns:
            pl.DataFrame with columns [tick, patch, S, E, I, R].
        """
        burnin_days = int(self.CONFIG["burnin_years"].default * 365)
        nodes = state.nodes

        rows = []
        for t in range(burnin_days, nodes.S.shape[0]):
            for p in range(nodes.S.shape[1]):
                rows.append({
                    "tick": t - burnin_days,
                    "patch": p,
                    "S": int(nodes.S[t, p]),
                    "E": int(nodes.E[t, p]),
                    "I": int(nodes.I[t, p]),
                    "R": int(nodes.R[t, p]),
                })

        return pl.DataFrame(rows)

    @model_scenario("baseline")
    def baseline(self) -> ScenarioSpec:
        """Baseline scenario with no parameter modifications."""
        return ScenarioSpec("baseline", param_patches={}, config_patches={})

    @model_scenario("no_seasonality")
    def no_seasonality(self) -> ScenarioSpec:
        """Scenario with seasonal forcing disabled."""
        return ScenarioSpec(
            "no_seasonality",
            param_patches={"seasonal_amplitude": 0.0},
            config_patches={},
        )
