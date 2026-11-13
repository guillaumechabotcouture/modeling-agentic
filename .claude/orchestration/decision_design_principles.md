Decision design principles (the owner's default for every run, 2026-11-12). Apply them when drafting a
spec, choosing an engine, critiquing, planning or making fixes, and writing. A departure from any of them is
declared in the spec with its reason, and the report repeats it; it is never silent. Where the harness cannot
yet compute what a principle asks for, that is such a departure: the spec declares it, and a critic reports
it as a declared limitation, never as a blocker a fix task is asked to resolve.

0. Think from first principles, and keep thinking from first principles. Before choosing a method, a
   structure, an input or a fix, ask what the decision needs and what the evidence can support, and reason
   from those, not from precedent, habit, the previous run or what the tools make easy. Ask again at every
   stage, including when a result looks right, when a finding seems minor, and when you are patching one
   problem at a time.

1. Start from the decision. Name the choice: the options, the budget, the units it is allocated across and
   the date it is made. Its benefit per cost decomposes into the baseline burden in each unit, times the
   relative effect of each option in that unit, set against each option's cost. Say which part drives the
   choice. Usually relative burden ranks the units, and differences in relative effect and in price rank
   the options.

2. Reprogramming from the decision date. The comparator is the plan as currently funded. When the funded
   plan differs between units and the spec's comparator is a single package, declare the funded plan as a
   fixed plan (a plan variant) and report the headline as the regret or the gain against it, saying which
   claim is the headline. A no-action counterfactual is context only, never the headline. An action already
   delivered before the decision date is a fixed input, not a decision: optimise only what remains.

3. Anchor each part to its strongest evidence.
   - Baseline burden (infections, cases, deaths) comes from data-driven external estimates: surveys,
     geospatial estimates, and national and international burden estimates, triangulated across sources.
     The engine supplies the relative reductions applied to that baseline. The engine's own burden is a
     validation check, not the basis of the decision.
   - A relative reduction is applied only to a baseline with the same comparator, outcome, population and
     time window. A baseline that already includes an intervention takes reductions relative to that
     intervention, never a reduction measured against no intervention, which would count its protection
     twice.
   - Relative effects of interventions come from randomised trials. The engine's intervention effects are
     calibrated to trial effect sizes. Its role is to carry them over to the setting (transmission level,
     seasonality, coverage, durability) and to handle interactions between interventions.
   - Costs are the current procurement and delivery prices for the period the decision buys, from the
     funder's own reference lists where they exist.

4. When the engine contradicts direct trial evidence on a quantity the decision turns on, the trial wins,
   unless the engine names a feature of the setting, backed by evidence, that explains the difference.
   Never tune an uncertain input until the engine gives the answer you expect.

5. When measured data and a modelled surface disagree about an input, compare them on the same estimand,
   after accounting for how each was measured: the sampling frame, ascertainment or reporting completeness,
   and known biases. Prefer the measured data when it is comparable on that basis, for example field
   surveillance over a modelled map of the same quantity. Record the conflict and both values.

6. An uncertain input that can move the decision is a range with a decision test, never a single point
   value.

7. Decide at the granularity the evidence supports. Judge robustness on the paired difference between
   options (or the regret, or how often the choice holds across uncertainty draws), never on whether the
   options' separate intervals overlap: shared uncertainty can leave two options' intervals overlapping
   while one wins in every draw. Where the paired difference does not favour one option reliably, report a
   robust rule at the level that holds (a region, or an intervention component) and mark finer lists as
   indicative. Give the regret against the funded plan. When the choice between options turns on price,
   report the price bracket the harness's price tests computed within which it flips, or that it was not
   computed; never interpolate a price that no computation produced.
