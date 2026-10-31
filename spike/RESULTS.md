# Architecture-v2 spike: results

Question: does "a validated, reusable engine plus analysis computed by harness code" beat
attempt 8's "write everything per run" on the same question (Nigeria GC7 malaria
allocation)? Design note: `spike/DESIGN.md`. Every number in the tables below is rendered
from artifacts by `spike/render_results.py`; none is typed by hand.

## Summary

SUMMARY_PLACEHOLDER

## What was run

1. **Engine** (`toolkit/engines/malaria/`): attempt 8's Starsim model promoted behind the
   `Engine` contract, with species-specific bionomics (D6 prior values; endophily assumed),
   ITN deflection / killing / feeding with resistance, PBO, dual-AI and decay, and IRS
   deterrence and killing of indoor-resting mosquitoes. No interaction factor.
2. **Benchmark pack** (`toolkit/engines/malaria/benchmarks/`): four engine-level parameters
   fitted once (sequential one-parameter grid fits, 4 seeds), then every benchmark run with
   8 seeds and common random numbers. B13, B15, B16c, B17–B20 and two added PBO durability
   checks (B9b, B9c) were held out of fitting. 634 engine runs for fit plus pack, on the
   laptop (12 cores).
3. **Decision spec** (`spike/decision_spec.yaml`): the 13 Global Fund states (309 LGAs,
   90.8M people), packages net type × SMC × IRS × enhanced CM (32), the attempt 8 cost
   spec in toolkit/alloc form, envelope $315.6M with the $59.3M CM floor fixed, undiscounted
   in-window DALYs vs CF1.
4. **Analysis** (`spike/analysis/`): 256 design points × 32 packages = 8,192 engine runs on
   six 8-vCPU Coiled VMs (`cloud/vm_job.py`), Gaussian-process emulators with held-out
   accuracy, calibration of each LGA's transmission to its 2021 PfPR target by emulator
   inversion, toolkit/alloc ILP, 64 uncertainty draws, leave-one-archetype-out, and decision
   test DT1.

## Process notes, stated plainly

- **The fit procedure was changed once, after its first pass.** The first fit refined each
  parameter with a parabola through the three best grid points. For the chlorfenapyr
  killing parameter this landed at 0.24, where B10 is out of band, although 0.30 puts both
  of its targets in band. The runner now interpolates each benchmark on a fine grid and
  takes the value with the fewest out-of-band targets, then the lowest loss. The pack was
  refitted once with the new rule; nothing else changed. The first fit is kept in the
  scratch log for comparison.
- **B20 was first reported as $0/DALY** because the runner charged the incremental net
  price on the difference in nets distributed (zero between two net arms). The runner
  gained a `costs_num` option (incremental price × quantity in the intervention arm) and
  the pack was re-run with the same seeds.
- Trial-site settings (species mix, resistance, seasonality, net use) are assumptions
  written from the trial descriptions. Each site's transmission is calibrated to its
  control-arm or baseline level; settings cannot override fitted parameters (the runner
  refuses).

<!-- tables:start -->
<!-- tables:end -->

## B15 and B19, in detail

B15_PLACEHOLDER

## What to change in the v2 design

DESIGN_PLACEHOLDER
