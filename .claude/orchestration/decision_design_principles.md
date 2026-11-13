Decision design principles (the owner's default for every run, 2026-11-12). Apply them when drafting a
spec, choosing an engine, critiquing, planning or making fixes, and writing. A departure from any of them is
stated in the spec or the report, with its reason; it is never silent.

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

2. Reprogramming from the decision date. The comparator is the plan as currently funded. A no-action
   counterfactual is context only, never the headline. An action already delivered before the decision
   date is a fixed input, not a decision: optimise only what remains.

3. Anchor each part to its strongest evidence.
   - Baseline burden (infections, cases, deaths) comes from data-driven external estimates: surveys,
     geospatial estimates, and national and international burden estimates, triangulated across sources.
     The engine supplies the relative reductions applied to that baseline. The engine's own burden is a
     validation check, not the basis of the decision.
   - Relative effects of interventions come from randomised trials. The engine's intervention effects are
     calibrated to trial effect sizes. Its role is to carry them over to the setting (transmission level,
     seasonality, coverage, durability) and to handle interactions between interventions.
   - Costs are the current procurement and delivery prices for the period the decision buys, from the
     funder's own reference lists where they exist.

4. When the engine contradicts direct trial evidence on a quantity the decision turns on, the trial wins,
   unless the engine names a feature of the setting, backed by evidence, that explains the difference.
   Never tune an uncertain input until the engine gives the answer you expect.

5. When measured data and a modelled surface disagree about an input, prefer the measured data, for example
   field surveillance over a modelled map. Record the conflict and both values.

6. An uncertain input that can move the decision is a range with a decision test, never a single point
   value.

7. Decide at the granularity the evidence supports. Where options sit within each other's uncertainty,
   report a robust rule at the level that holds (a region, or an intervention component) and mark finer
   lists as indicative. Give the regret against the funded plan. When the choice between options turns on
   price, report the price at which it flips.
