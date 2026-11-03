#!/usr/bin/env python3
"""Self-test toy engines for toolkit/spec/engine_parameters.py (and the sign-off sheet's self-test).

Never allowed in a production run: toolkit/import_guard.py refuses these references unless a self-test enables them
with `import_guard.allow_test_refs("engine", REFS)` (that process and the worker processes it starts). ToySpecEngine
appends to the log path its configuration names; nothing outside a self-test can construct it through the guard.
Exercised by the self-test of toolkit/spec/engine_parameters.py.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.engines.contract import ParamSpec, resolve_params  # noqa: E402
from toolkit.engines.empirical.engine import PackageRateEngine  # noqa: E402
from toolkit.engines.hooks import ToyScheduleEngine  # noqa: E402

REFS = ("toolkit.spec.toy_engines:ToySpecEngine", "toolkit.spec.toy_engines:ToySpecRateEngine")
LOG_ENV = "MA_TOY_SPEC_LOG_DIR"      # set by the self-test only: the directory of the call log (its trusted root)
LOG_FILE = "calls.{pid}.jsonl"


class ToySpecEngine(ToyScheduleEngine):
    """The toy engine with both hooks, a card range on `base` ([0.4, 0.6]) and a calibrated parameter `c_rate` (unused
    by the model). `log`: every run, schedule and run_series call appends the resolved base, k and synergy as a JSON
    line to calls.<pid>.jsonl under the directory the self-test names in LOG_ENV (worker processes inherit it), through
    lib.safe_io with that directory as the trusted root. No path comes from the configuration."""

    def __init__(self, results_path: str | None = None, log: bool = False):
        super().__init__(results_path)
        self.log = bool(log)

    def parameters(self) -> list:
        out = [ParamSpec("base", 0.5, "fixed", lo=0.4, hi=0.6, source="toy card range") if p.name == "base" else p
               for p in super().parameters()]
        return out + [ParamSpec("c_rate", 1.0, "calibrated", source="toy")]

    def _log(self, call, params):
        root = os.environ.get(LOG_ENV)
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
