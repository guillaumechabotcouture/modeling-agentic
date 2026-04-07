#!/usr/bin/env python3
"""
Custom LASER components for spatial SEIR modeling with seasonal transmission,
disease importation, and vaccination campaigns.

Components:
    Importation - Seeds infections in *susceptible* agents periodically to
                  sustain epidemics in sub-critical-community-size populations.
                  Unlike the built-in Infect_Random_Agents (laser.generic.importation),
                  this only infects susceptible agents, which is more epidemiologically
                  precise. Use the built-in class for simpler setups where state
                  filtering is not required.

    VaccinationCampaign - Periodic vaccination that correctly sets state=RECOVERED
                          (unlike built-in ImmunizationCampaign which only sets
                          susceptibility=0, having no effect on Transmission kernels).
                          Supports correlated missedness via per-agent `reachable` flag
                          for modeling hard-to-reach populations.

    SeasonalTransmission - Extends SEIR.Transmission with time-varying beta
                           using a seasonal forcing profile and spatial coupling.
                           NOTE (v1.0.0): The built-in TransmissionSE (aliased as
                           SEIR.Transmission) now accepts a `seasonality` parameter
                           (ValuesMap or ndarray) and handles spatial network coupling
                           internally. For most use cases, pass seasonality directly:
                             seasonality = ValuesMap.from_timeseries(beta_season_tiled, nnodes)
                             SEIR.Transmission(model, expdurdist, seasonality=seasonality)
                           Use this custom class only when you need non-standard behavior
                           (e.g., tick % 365 cycling, per-node profiles, modified coupling).

Usage:
    from custom_components import Importation, VaccinationCampaign, SeasonalTransmission
"""

import numpy as np
import numba as nb
import laser.core.distributions as dists
from laser.generic import SEIR


class Importation:
    """Seeds infections in susceptible agents periodically to sustain epidemics
    in sub-CCS populations.

    Unlike the built-in Infect_Random_Agents, this class filters agents by
    susceptible state before infecting, preventing wasted importation events
    on already-infected or recovered agents.

    Parameters:
        model: LASER Model instance
        infdurdist: Distribution for infectious duration sampling
        infdurmin: Minimum infectious duration in ticks (default: 1)
        period: Ticks between importation events (default: 30)
        count: Number of agents to infect per event (default: 3)
        end_tick: Stop importation after this tick (default: 10*365)
    """

    def __init__(self, model, infdurdist, infdurmin=1, period=30,
                 count=3, end_tick=10*365):
        self.model = model
        self.infdurdist = infdurdist
        self.infdurmin = infdurmin
        self.period = period
        self.count = count
        self.end_tick = end_tick or model.params.nticks
        self.model.nodes.add_vector_property(
            "imports", model.params.nticks + 1, dtype=np.uint32, default=0
        )

    def step(self, tick):
        if tick > 0 and tick % self.period == 0 and tick < self.end_tick:
            i_susceptible = np.nonzero(
                self.model.people.state == SEIR.State.SUSCEPTIBLE.value
            )[0]
            if len(i_susceptible) > 0:
                count = min(self.count, len(i_susceptible))
                i_infect = np.random.choice(i_susceptible, size=count, replace=False)
                self.model.people.state[i_infect] = SEIR.State.INFECTIOUS.value
                samples = dists.sample_floats(
                    self.infdurdist, np.zeros(count, np.float32)
                )
                samples = np.maximum(
                    np.round(samples), self.infdurmin
                ).astype(self.model.people.itimer.dtype)
                self.model.people.itimer[i_infect] = samples
                inf_by_node = np.bincount(
                    self.model.people.nodeid[i_infect],
                    minlength=len(self.model.nodes)
                ).astype(self.model.nodes.S.dtype)
                self.model.nodes.S[tick + 1] -= inf_by_node
                self.model.nodes.I[tick + 1] += inf_by_node
                self.model.nodes.imports[tick] = inf_by_node


class VaccinationCampaign:
    """Periodic vaccination campaign that correctly sets state=RECOVERED.

    Unlike the built-in ImmunizationCampaign (which only sets susceptibility=0
    and has NO effect on Transmission kernels), this component sets
    state=State.RECOVERED and updates node-level S/R counts.

    Supports **correlated missedness**: a fraction of agents per node are
    permanently marked unreachable at birth, modeling communities that are
    systematically missed by every campaign round (hard-to-reach areas, refusal,
    inaccessible terrain). This prevents independent Bernoulli draws from
    overestimating cumulative coverage across multiple rounds.

    Use correlated missedness when:
        - Vaccine access is heterogeneous (some communities always missed)
        - Multiple campaign rounds target the same population
        - Independent draws would overestimate coverage (e.g., 3 rounds at 80%
          would give 99.2% coverage independently, but real coverage may be ~85%)

    Use independent draws (unreachable_frac=0) when:
        - Vaccine access is approximately uniform
        - Each round independently samples the population

    Parameters:
        model: LASER Model instance
        period: Ticks between campaign rounds (e.g., 180 for biannual)
        coverage: Per-node coverage array, shape (nnodes,), values in [0, 1]
        age_lower: Minimum age in days for targeting (default: 0)
        age_upper: Maximum age in days for targeting (default: 5*365)
        start_tick: First tick campaigns begin (default: 0)
        end_tick: Last tick for campaigns (default: -1, meaning entire simulation)
        unreachable_frac: Per-node fraction of agents permanently unreachable,
                          shape (nnodes,) or scalar (default: 0.0, no correlated missedness)
    """

    def __init__(self, model, period=180, coverage=None, age_lower=0,
                 age_upper=5*365, start_tick=0, end_tick=-1,
                 unreachable_frac=0.0):
        self.model = model
        self.period = period
        self.coverage = coverage if coverage is not None else np.full(
            model.nodes.count, 0.8
        )
        self.age_lower = age_lower
        self.age_upper = age_upper
        self.start_tick = start_tick
        self.end_tick = end_tick if end_tick >= 0 else model.params.nticks
        self.unreachable_frac = np.broadcast_to(
            np.asarray(unreachable_frac, dtype=np.float32),
            (model.nodes.count,)
        ).copy()

        # Per-agent reachable flag (1 = reachable, 0 = permanently unreachable)
        model.people.add_scalar_property("reachable", dtype=np.int8, default=1)
        # Mark initial population as unreachable based on per-node fractions
        if np.any(self.unreachable_frac > 0):
            self._set_initial_reachability()

    def _set_initial_reachability(self):
        """Mark a fraction of existing agents per node as permanently unreachable."""
        people = self.model.people
        for node in range(self.model.nodes.count):
            agents_in_node = np.nonzero(people.nodeid[:people.count] == node)[0]
            n_unreachable = int(round(self.unreachable_frac[node] * len(agents_in_node)))
            if n_unreachable > 0:
                chosen = np.random.choice(agents_in_node, size=n_unreachable, replace=False)
                people.reachable[chosen] = 0

    def on_birth(self, istart, iend, tick):
        """Set reachability for newborns based on their node's unreachable fraction."""
        people = self.model.people
        for i in range(istart, iend):
            node = people.nodeid[i]
            if np.random.random() < self.unreachable_frac[node]:
                people.reachable[i] = 0

    def step(self, tick):
        if tick < self.start_tick or tick >= self.end_tick:
            return
        if (tick - self.start_tick) % self.period != 0:
            return

        people = self.model.people
        for node in range(self.model.nodes.count):
            # Find susceptible, reachable, age-eligible agents in this node
            mask = (
                (people.nodeid[:people.count] == node) &
                (people.state[:people.count] == SEIR.State.SUSCEPTIBLE.value) &
                (people.reachable[:people.count] == 1)
            )
            age = tick - people.dob[:people.count]
            mask &= (age >= self.age_lower) & (age < self.age_upper)
            eligible = np.nonzero(mask)[0]

            if len(eligible) == 0:
                continue

            # Bernoulli draw at node-specific coverage
            draws = np.random.random(len(eligible)) < self.coverage[node]
            vaccinated = eligible[draws]

            if len(vaccinated) > 0:
                people.state[vaccinated] = SEIR.State.RECOVERED.value
                count = len(vaccinated)
                self.model.nodes.S[tick + 1] -= count
                self.model.nodes.R[tick + 1] += count


class SeasonalTransmission(SEIR.Transmission):
    """Extends SEIR.Transmission with time-varying beta via seasonal forcing.

    ADVANCED CUSTOMIZATION EXAMPLE. For most use cases, prefer the built-in
    seasonality parameter on SEIR.Transmission (TransmissionSE):
        seasonality = ValuesMap.from_timeseries(beta_season_tiled, nnodes)
        SEIR.Transmission(model, expdurdist, seasonality=seasonality)

    This custom class differs from the built-in in that it:
    - Uses tick % 365 cycling (auto-repeats the 365-day profile)
    - Reads beta_season from model.params rather than a ValuesMap

    Expects model.params to contain:
        beta: Base transmission rate
        beta_season: 365-element array of seasonal modulation factors

    Expects model.network to be set (gravity coupling matrix).
    """

    def step(self, tick):
        ft = self.model.nodes.forces[tick]
        N = (self.model.nodes.S[tick] + self.model.nodes.E[tick] +
             (I := self.model.nodes.I[tick]))
        if hasattr(self.model.nodes, "R"):
            N += self.model.nodes.R[tick]

        # Seasonal beta modulation
        ft[:] = (self.model.params.beta * I / N *
                 self.model.params.beta_season[tick % 365])

        # Spatial coupling via network
        transfer = ft[:, None] * self.model.network
        ft += transfer.sum(axis=0)
        ft -= transfer.sum(axis=1)
        ft = -np.expm1(-ft)  # Convert rate to probability

        newly_infected_by_node = np.zeros(
            (nb.get_num_threads(), self.model.nodes.count), dtype=np.int32
        )
        self.nb_transmission_step(
            self.model.people.state, self.model.people.nodeid, ft,
            newly_infected_by_node, self.model.people.etimer,
            self.expdurdist, self.expdurmin, tick,
        )
        newly_infected_by_node = newly_infected_by_node.sum(axis=0).astype(
            self.model.nodes.S.dtype
        )
        self.model.nodes.S[tick + 1] -= newly_infected_by_node
        self.model.nodes.E[tick + 1] += newly_infected_by_node
        self.model.nodes.newly_infected[tick] = newly_infected_by_node
