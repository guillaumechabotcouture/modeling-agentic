# Skills Index

19 skills live here. `v2`'s spec task loads `decision-spec` by path, `tools/run_console` references `critique-blockers-schema`, and engine-build tasks may read `starsim-dev*`. Each skill is a directory with a `SKILL.md` that opens with YAML frontmatter (`name` matching the directory, `description`) followed by the guidance body. `python3 scripts/validate_skill_attachments.py` fails on phantom attachments and on missing or malformed frontmatter; add `--show-orphans` to list skills no module under `agents/` loads.

## Core & Schema

- **decision-spec/** — Drafting `decision_spec.yaml`: the owner-signed spec the harness analysis engine (`toolkit/analysis`) runs from; uncertainty sources declared up front, group rules, decision tests.
- **critique-blockers-schema/** — Required schema for `critique_*.yaml`: blocker IDs, severities, `target_stage`, categories, `structural_mismatch`, and how the validator computes `unresolved_high` from them.

## Frameworks (`starsim-dev*`)

- **starsim-dev/** — Framework index skill.
  - **starsim-dev-intro/** — Architecture overview.
  - **starsim-dev-sim/** — Simulation lifecycle.
  - **starsim-dev-diseases/** — Component classes and state templates.
  - **starsim-dev-networks/** — Contact networks.
  - **starsim-dev-interventions/** — Scheduled and event-triggered interventions.
  - **starsim-dev-demographics/** — Births, deaths, aging.
  - **starsim-dev-calibration/** — Built-in calibration.
  - **starsim-dev-analyzers/** — Result extraction and post-hoc analysis.
  - **starsim-dev-time/** — Time units and conversion.
  - **starsim-dev-random/** — Random state, seeding, reproducibility.
  - **starsim-dev-distributions/** — Parametric distributions.
  - **starsim-dev-connectors/** — Multi-component connectors.
  - **starsim-dev-nonstandard/** — Non-standard usage patterns.
  - **starsim-dev-indexing/** — Agent state indexing.
  - **starsim-dev-profiling/** — Profiling and performance.
  - **starsim-dev-run/** — Running and parameter sweeps.
