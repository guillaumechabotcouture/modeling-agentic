---
phase: "Agent-built engines (sandbox and admission follow-up)"
commit: "fix minimal-mount bubblewrap worker mounts, handle calibrated attributes in A1 real units, clarify build/reference contract prompts, and exercise bubblewrap in CI"
---

## Changelog

**Fix sandbox probe, A1 calibrated-attribute binding, and build/reference prompt gaps surfaced on the second cloud smoke of the agent-built engine lane (`#363`).**

- `toolkit/engines/built/worker.py`: remove the invalid `--ro-bind /dev/null` mounts on `/harness/toolkit/__init__.py` (using PEP 420 implicit namespace packages with `--remount-ro /harness`), mount dynamic-linker config and safe `LD_LIBRARY_PATH` / interpreter prefixes in parent-first depth order, ensure `/usr/bin:/bin` on `PATH` for `setpriv`, verify `toolkit.engines.contract` import inside `_probe`, record probe failure diagnostics in `EngineUnsandboxed`, and enforce `MA_REQUIRE_SANDBOX=1` in `BE18`.
- `toolkit/engines/built/admission.py`: in `_real_units_and_packages`, populate a spec's `calibration.attribute` with its prior/range interior midpoint (`exp(mid)` when `scale: log`, clamped to attribute bounds) instead of failing A1 with a missing-binding error when the calibrated attribute is intentionally unbound in `decision_units.engine_attributes`. Adds self-test `BE_CAL_ATTR1`.
- `agents/v2_sdk_tasks.py`: pass the absolute repo path `{repo}` into `build_engine` and `build_reference` prompts, specify exact `toolkit.engines.contract` dataclass signatures, `Model` entrypoints (`evaluate` for `closed_form` and A11 reference; `state_vars`/`build_sim`/`read_outcomes` for `starsim_module`), and S3/S4 top-level module restrictions; add `draft_spec` guidance on unambiguous `(output, age_group)` semantics for A11 and `nonzero` for non-monotone effects.
- `scripts/pipeline.py` and `.github/workflows/ci.yml`: forward `LD_LIBRARY_PATH` into the derivation sandbox and install `bubblewrap` with `MA_REQUIRE_SANDBOX=1` in GitHub Actions CI so both `worker.py` (`BE18`) and `scripts/pipeline.py` (`DI2`–`DI4`) execute inside real Linux `bwrap`/`bwrap-sudo` on every PR.
