#!/usr/bin/env python3
"""
Post-run model health checks for LASER spatial SEIR models.

Provides reusable validation functions to verify that a LASER model
is producing epidemiologically sensible output. Run after model.run()
to catch silent failures like wrong units, vaccination bugs, or
disconnected spatial networks.

Usage:
    from verification_checks import verify_model_health

    model.run("Simulation")
    verify_model_health(model)  # Raises on critical failures

Each check returns {"passed": bool, "message": str, "details": dict}.
"""

import numpy as np


def check_population_trajectory(model):
    """Population at end vs start — catches wrong birthrate units.

    If birthrates are in wrong units (daily per-capita instead of
    per-1000/year), calc_capacity sees near-zero growth, capacity equals
    initial population, and no births occur. The population stays static
    or declines from mortality.

    A healthy model with births should show population growth over time.
    """
    nodes = model.nodes
    nticks = model.params.nticks

    # Sum population across all patches at start and end
    pop_start = (
        nodes.S[1].sum() + nodes.E[1].sum()
        + nodes.I[1].sum() + nodes.R[1].sum()
    )
    pop_end = (
        nodes.S[nticks - 1].sum() + nodes.E[nticks - 1].sum()
        + nodes.I[nticks - 1].sum() + nodes.R[nticks - 1].sum()
    )

    ratio = pop_end / pop_start if pop_start > 0 else 0.0
    years = nticks / 365

    # With any positive birth rate, population should grow over multi-year sims
    if years > 5 and abs(ratio - 1.0) < 0.001:
        return {
            "passed": False,
            "message": (
                f"Population static over {years:.0f} years "
                f"(ratio={ratio:.4f}). Likely wrong birthrate units — "
                f"must be per-1000/year, not daily per-capita."
            ),
            "details": {
                "pop_start": int(pop_start),
                "pop_end": int(pop_end),
                "ratio": ratio,
                "years": years,
            },
        }

    return {
        "passed": True,
        "message": f"Population ratio={ratio:.3f} over {years:.0f} years.",
        "details": {
            "pop_start": int(pop_start),
            "pop_end": int(pop_end),
            "ratio": ratio,
        },
    }


def check_compartment_nonnegativity(model):
    """Sample S/E/I/R at intervals — catches depletion bugs.

    Custom components that decrement compartment counts without
    non-negativity guards can drive counts below zero. This check
    samples at regular intervals to detect the problem.
    """
    nodes = model.nodes
    nticks = model.params.nticks

    # Sample every 10% of the simulation
    sample_ticks = np.linspace(1, nticks - 1, 10, dtype=int)
    negative_found = []

    for t in sample_ticks:
        for comp_name in ["S", "E", "I", "R"]:
            if hasattr(nodes, comp_name):
                vals = getattr(nodes, comp_name)[t]
                min_val = vals.min()
                if min_val < 0:
                    negative_found.append(
                        {"tick": int(t), "compartment": comp_name, "min": int(min_val)}
                    )

    if negative_found:
        return {
            "passed": False,
            "message": (
                f"Negative compartment counts found at {len(negative_found)} "
                f"sample points. First: {negative_found[0]}. "
                f"Add non-negativity guards in custom components."
            ),
            "details": {"violations": negative_found},
        }

    return {
        "passed": True,
        "message": "No negative compartment counts at sampled ticks.",
        "details": {"ticks_checked": len(sample_ticks)},
    }


def check_vaccination_effect(model):
    """Compare susceptible fraction early vs late — catches susceptibility bug.

    If vaccination sets susceptibility=0 instead of state=RECOVERED, the
    Transmission kernel still treats agents as susceptible. The susceptible
    fraction will not decrease as expected over time with ongoing vaccination.

    This check compares the susceptible fraction in the first and second
    halves of the simulation. With working vaccination, it should decrease.
    """
    nodes = model.nodes
    nticks = model.params.nticks

    # Compare first quarter vs last quarter
    q1_end = nticks // 4
    q4_start = 3 * nticks // 4

    def susc_frac(t):
        s = nodes.S[t].sum()
        total = s + nodes.E[t].sum() + nodes.I[t].sum() + nodes.R[t].sum()
        return s / total if total > 0 else 0.0

    # Average susceptible fraction in first and last quarters
    early_ticks = np.linspace(1, q1_end, 5, dtype=int)
    late_ticks = np.linspace(q4_start, nticks - 1, 5, dtype=int)

    early_frac = np.mean([susc_frac(t) for t in early_ticks])
    late_frac = np.mean([susc_frac(t) for t in late_ticks])

    # This is a soft check — not all models have vaccination
    # Only flag if susceptible fraction hasn't changed at all
    details = {"early_frac": float(early_frac), "late_frac": float(late_frac)}

    if early_frac > 0.1 and abs(early_frac - late_frac) < 0.001:
        return {
            "passed": False,
            "message": (
                f"Susceptible fraction unchanged ({early_frac:.3f} -> "
                f"{late_frac:.3f}). If vaccination is active, it may be "
                f"setting susceptibility instead of state=RECOVERED."
            ),
            "details": details,
        }

    return {
        "passed": True,
        "message": (
            f"Susceptible fraction: {early_frac:.3f} (early) -> "
            f"{late_frac:.3f} (late)."
        ),
        "details": details,
    }


def check_spatial_coupling(model):
    """Verify network sum > 0 and epidemic timing varies across patches.

    If the gravity network is all zeros (gravity_k too small or network
    not set), patches evolve independently. This produces identical
    epidemic timing across all patches (no traveling waves).
    """
    network = model.network
    nticks = model.params.nticks
    nnodes = model.nodes.count

    # Check network is non-trivial
    network_sum = float(network.sum())
    max_row_sum = float(network.sum(axis=1).max())

    if network_sum == 0:
        return {
            "passed": False,
            "message": (
                "Migration network is all zeros — patches are isolated. "
                "Check gravity_k parameter (typical range 0.001-0.1)."
            ),
            "details": {"network_sum": 0.0, "max_row_sum": 0.0},
        }

    # Check epidemic timing variation across patches
    # Find the tick of peak incidence in each patch
    if hasattr(model.nodes, "newly_infected"):
        incidence = model.nodes.newly_infected[:nticks, :]
        peak_ticks = np.argmax(incidence, axis=0)
        timing_spread = float(np.std(peak_ticks))

        if nnodes > 1 and timing_spread < 1.0:
            return {
                "passed": False,
                "message": (
                    f"All {nnodes} patches peak at the same tick "
                    f"(spread={timing_spread:.1f}). Network may be too weak."
                ),
                "details": {
                    "network_sum": network_sum,
                    "max_row_sum": max_row_sum,
                    "timing_spread": timing_spread,
                },
            }

    return {
        "passed": True,
        "message": (
            f"Network active (sum={network_sum:.4f}, "
            f"max_row={max_row_sum:.4f})."
        ),
        "details": {
            "network_sum": network_sum,
            "max_row_sum": max_row_sum,
        },
    }


def check_epidemic_dynamics(model):
    """Total incidence > 0, temporal variation, persistence.

    Catches beta-too-low (no epidemic) and missing importation
    (disease dies out and never returns).
    """
    nticks = model.params.nticks

    if not hasattr(model.nodes, "newly_infected"):
        return {
            "passed": True,
            "message": "No newly_infected array — skipping dynamics check.",
            "details": {},
        }

    incidence = model.nodes.newly_infected[:nticks, :]
    total_incidence = int(incidence.sum())

    if total_incidence == 0:
        return {
            "passed": False,
            "message": (
                "Zero total incidence — no infections occurred. "
                "Check beta (too low?), initial I counts, and importation."
            ),
            "details": {"total_incidence": 0},
        }

    # Check temporal variation (not just a single burst)
    num_weeks = incidence.shape[0] // 7
    weekly = incidence[: num_weeks * 7, :].reshape(num_weeks, 7, incidence.shape[1]).sum(axis=(1, 2))
    nonzero_weeks = int(np.count_nonzero(weekly))
    total_weeks = len(weekly)
    active_frac = nonzero_weeks / total_weeks

    # Check persistence — disease should be active in more than just early weeks
    last_quarter = weekly[3 * total_weeks // 4 :]
    late_activity = int(np.count_nonzero(last_quarter))

    details = {
        "total_incidence": total_incidence,
        "active_weeks": nonzero_weeks,
        "total_weeks": total_weeks,
        "active_fraction": float(active_frac),
        "late_active_weeks": late_activity,
    }

    if active_frac < 0.05:
        return {
            "passed": False,
            "message": (
                f"Epidemic active in only {nonzero_weeks}/{total_weeks} weeks "
                f"({active_frac:.1%}). Disease may have died out. "
                f"Check importation period and beta."
            ),
            "details": details,
        }

    if late_activity == 0:
        return {
            "passed": False,
            "message": (
                f"No activity in last quarter ({total_weeks // 4} weeks). "
                f"Disease died out — extend importation or increase beta."
            ),
            "details": details,
        }

    return {
        "passed": True,
        "message": (
            f"Total incidence={total_incidence:,}, "
            f"active in {active_frac:.0%} of weeks, "
            f"late activity={late_activity} weeks."
        ),
        "details": details,
    }


def verify_model_health(model, raise_on_critical=True):
    """Run all checks, print report, optionally raise on critical failures.

    Args:
        model: LASER Model after run() has completed.
        raise_on_critical: If True, raise RuntimeError on any failed check.

    Returns:
        dict mapping check name to result dict.
    """
    checks = {
        "population_trajectory": check_population_trajectory,
        "compartment_nonnegativity": check_compartment_nonnegativity,
        "vaccination_effect": check_vaccination_effect,
        "spatial_coupling": check_spatial_coupling,
        "epidemic_dynamics": check_epidemic_dynamics,
    }

    results = {}
    failures = []

    print("\n=== Model Health Report ===")
    for name, check_fn in checks.items():
        result = check_fn(model)
        results[name] = result
        status = "PASS" if result["passed"] else "FAIL"
        print(f"  [{status}] {name}: {result['message']}")
        if not result["passed"]:
            failures.append(name)

    if failures:
        print(f"\n  {len(failures)} check(s) failed: {', '.join(failures)}")
        if raise_on_critical:
            raise RuntimeError(
                f"Model health check failed: {', '.join(failures)}. "
                f"See report above for details."
            )
    else:
        print("\n  All checks passed.")

    print("=" * 30)
    return results
