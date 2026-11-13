#!/usr/bin/env python3
"""Self-test toy engines for toolkit/spec/engine_parameters.py (and the sign-off sheet's self-test).

Never allowed in a production run: toolkit/import_guard.py refuses these references unless code in a self-test
enables them (`enable_for_test`, which calls `import_guard.allow_test_refs`); its worker processes get that registration
only as their pool initializer's argument (`import_guard.pool_kwargs`). No environment variable enables them.
ToySpecEngine appends to a call log under the root the self-test registered; nothing outside a self-test can construct
it through the guard.
Exercised by the self-test of toolkit/spec/engine_parameters.py.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.engines.contract import ParamSpec, ToyEngine, resolve_params  # noqa: E402
from toolkit.engines.empirical.engine import PackageRateEngine  # noqa: E402
from toolkit.engines.hooks import ToyScheduleEngine  # noqa: E402

REFS = ("toolkit.spec.toy_engines:ToySpecEngine", "toolkit.spec.toy_engines:ToySpecRateEngine",
        "toolkit.spec.toy_engines:ToyDossierEngine")
LOG_STATE = "toy_spec_log_root"      # the call log's root, registered by the self-test (never read from the environment)
LOG_FILE = "calls.{pid}.jsonl"


def enable_for_test(log_root: str | None = None) -> None:
    """Self-tests only: enable REFS in this process (import_guard.allow_test_refs) and, when given, root the call log
    in `log_root`, a temporary directory the self-test created. Both travel to worker processes only through
    import_guard.pool_kwargs()."""
    from toolkit import import_guard as G
    G.allow_test_refs("engine", REFS, state={LOG_STATE: os.path.realpath(log_root) if log_root else None})


class ToySpecEngine(ToyScheduleEngine):
    """The toy engine with both hooks, a card range on `base` ([0.4, 0.6]) and a calibrated parameter `c_rate` (unused
    by the model). `log`: every run, schedule and run_series call appends the resolved base, k and synergy as a JSON
    line to calls.<pid>.jsonl under the root the self-test registered (enable_for_test; its workers get it from their
    pool initializer), through lib.safe_io with that directory as the trusted root. No path comes from the
    configuration or the environment. Its benchmark ids (fit targets TF1, TF2; held out TH1, TH2) are in
    toy_engines_pack.yaml beside this file (engine_parameters.benchmark_pack)."""

    BENCHMARK_PACK = "toy_engines_pack.yaml"

    def __init__(self, results_path: str | None = None, log: bool = False):
        super().__init__(results_path)
        self.log = bool(log)

    def parameters(self) -> list:
        out = [ParamSpec("base", 0.5, "fixed", lo=0.4, hi=0.6, source="toy card range") if p.name == "base" else p
               for p in super().parameters()]
        return out + [ParamSpec("c_rate", 1.0, "calibrated", source="toy")]

    def _log(self, call, params):
        from toolkit import import_guard as G
        root = G.test_state(LOG_STATE)
        if self.log and root:
            if os.path.join(ROOT, "scripts") not in sys.path:
                sys.path.insert(0, os.path.join(ROOT, "scripts"))
            from lib.safe_io import write_nofollow
            p = resolve_params(self, params)
            line = json.dumps({"call": call, **{k: p[k] for k in ("base", "k", "synergy")}}) + "\n"
            # one file per process: worker processes never create the same file at once
            write_nofollow(root, LOG_FILE.format(pid=os.getpid()), line.encode(), append=True)

    def run(self, params, unit, package, horizon, seed):
        self._log("run", params)
        return super().run(params, unit, package, horizon, seed)

    def schedule(self, params, unit, package, horizon):
        self._log("schedule", params)
        return super().schedule(params, unit, package, horizon)

    def run_series(self, params, unit, packages, horizon, seed, metrics, step="month"):
        self._log("run_series", params)
        return super().run_series(params, unit, packages, horizon, seed, metrics, step)


class ToySpecRateEngine(PackageRateEngine):
    """The package-rate engine with a fixed multiplier `scale` on the rate (no card range). In-process runs record the
    resolved scale in CALLS (the direct path's self-test)."""
    CALLS: list = []

    def parameters(self) -> list:
        return super().parameters() + [ParamSpec("scale", 1.0, "fixed", "multiplier", source="toy")]

    def run(self, params, unit, package, horizon, seed):
        p = resolve_params(self, params)
        ToySpecRateEngine.CALLS.append(p["scale"])
        o = super().run({k: v for k, v in p.items() if k != "scale"}, unit, package, horizon, seed)
        o.per_capita = {k: {g: v * p["scale"] for g, v in d.items()} for k, d in o.per_capita.items()}
        o.windows = {w: {m: v * p["scale"] for m, v in d.items()} for w, d in o.windows.items()}
        return o


class ToyDossierEngine(ToyEngine):
    """The toy engine whose component `a` declares decision inputs (the fitted effect eff_a and the unit attribute x;
    none when a = 0), and `b` none, with its reason: the evidence dossier's self-tests (toolkit/spec/evidence_dossier.py,
    toolkit/spec/signoff.py), as a class attribute (toolkit/engines/decision_inputs.py)."""

    DECISION_INPUTS = {
        "a": {"inactive": [0], "inputs": [
            {"kind": "parameter", "name": "eff_a", "why": "the relative reduction component a gives"},
            {"kind": "unit_attribute", "name": "x", "why": "the transmission index the reduction acts on"}]},
        "b": {"inputs": [], "none_reason": "toy: the effect of b is not checked against field data"}}
