---
phase: "Agent-built engines (PR alpha)"
commit: "engine.build request schema, RequestedEngine stand-in, full-registry justification check, sheet digest in sign-off record, and --signoff none refusal"
---

## Changelog

**Add the pre-sign contract for agent-built engines (PR alpha of `#363`).**

- `toolkit/engines/built/__init__.py` and `toolkit/engines/built/requested.py`: add `validate_build()` for `engine.build` specs (components, typed unit attributes, outputs, parameters, attribute binding, decision inputs, expected effects, inert/parameter roles, compartment transition graph with per-capita transition coverage, oracle reductions, structured trial declarations, reference tolerance ceiling, and reserved harness config keyword refusal), `RequestedEngine` stand-in (implementing `Engine` while raising `EngineNotBuilt` on `run()`), `sheet_lines()` for the pre-sign check plan, and the Starsim unrun-`Sim` analyzer spike. Self-test `BE24`, `BE_SPIKE`.
- `toolkit/spec/schema.py`, `toolkit/spec/preconditions.py`, and `toolkit/spec/evidence_dossier.py`: accept `engine.build` (plain and `auto` + `build`) pending build/admission, validate engine-dependent spec sections before sign-off via `with_engine(spec, RequestedEngine)` and `load_binding`, resolve requested `decision_inputs` directly from the signed request, and refuse direct naming of `toolkit.engines.built:*` outside the build form.
- `agents/analysis_backend.py` (`build_request_problems`): verify `engine.build` justification against the full registered `ENGINES` list regardless of any `candidates` shortlist. Self-test `BE25`.
- `toolkit/spec/signoff.py`: persist `decision_spec.signoff_sheet.md`'s sha256 in every spec sign-off record and render the `## Engine to be built` section (request table, check plan, uncovered outputs/components, and full-registry rows). Self-test `BE26`.
- `agents/stage_machine.py`: refuse `engine.build` specs under `--signoff none` at SPEC (and fail closed at SIGN). Self-test `BE27`.
