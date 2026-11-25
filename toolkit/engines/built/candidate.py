#!/usr/bin/env python3
"""Harness-only CandidateEngine for unadmitted agent-built snapshots (docs/design/agent-built-engines.md §2.2 & §5.1).

`toolkit.engines.built.candidate` is unconditionally refused by `toolkit/import_guard.py` for EVERY
policy (BE36), so no spec, benchmark pack, or user ref can ever import or construct `CandidateEngine`.
Only harness code (admission and the try tool) constructs it directly from verified snapshot bytes.
It executes through the exact same minimal-mount bubblewrap worker as `BuiltEngine` and reports the
identical `code_fingerprint`, `identity_extra`, and public `ENGINE_REF` so benchmark packs stamped
during admission match `BuiltEngine` verbatim. Nothing here names a disease.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Mapping

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from toolkit.engines.contract import EngineCard, Horizon, Outcomes, Package, ParamSpec, UnitSpec  # noqa: E402
from toolkit.engines.built import adapter as BADAPT  # noqa: E402
from toolkit.engines.built import requested as BREQ  # noqa: E402
from toolkit.engines.built import worker as BWORK  # noqa: E402


class CandidateEngine:
    """Sandboxed Engine-contract wrapper for an unadmitted snapshot during admission and try calls."""

    ENGINE_REF = "toolkit.engines.built:BuiltEngine"
    _direct_runner = True

    def __init__(
        self,
        verified_files: Mapping[str, bytes],
        build_decl: Mapping,
        config: Mapping | None = None,
        *,
        run_dir: str | None = None,
        unsandboxed_ok: bool = False,
        limits: Mapping | None = None,
        hash_seed: str = "0",
        id_salt: str = "s0",
        fitted_path: str | None = None,
        results_path: str | None = None,
        pack_path: str | None = None,
        coverage_path: str | None = None,
    ):
        self._files = {str(k): bytes(v) for k, v in verified_files.items()}
        self._build = dict(build_decl)
        self._req_engine = BREQ.RequestedEngine(self._build)
        self._request_sha = BREQ.request_sha256(self._build)
        self.id = self._req_engine.id
        self.kind = self._req_engine.kind
        self._config = dict(config or {})
        self._config_sha = BADAPT.effective_config_sha256(self._config)
        self._code_sha256 = BADAPT._compute_code_sha256(self._files)
        self.run_dir = os.path.abspath(run_dir) if run_dir else None
        self._unsandboxed_ok = bool(unsandboxed_ok)
        self._limits = dict(limits) if limits else None
        self._hash_seed = str(hash_seed)
        self._id_salt = str(id_salt)
        self._force_sandbox: str | None = "auto"
        self.used_unsandboxed = False
        self.fitted_path = fitted_path
        self.results_path = results_path
        self.pack_path = pack_path
        self.coverage_path = coverage_path
        self._card_cache: EngineCard | None = None
        self._params_cache: list[ParamSpec] | None = None
        self._has_batch_cache: bool | None = None

    def config(self) -> dict:
        return dict(self._config)

    def code_fingerprint(self) -> str:
        return self._code_sha256

    def identity_extra(self) -> dict:
        return {
            "request_sha256": self._request_sha,
            "effective_config_sha256": self._config_sha,
            "starsim_version": BADAPT.starsim_version(),
            "deps_sha256": BADAPT.deps_sha256(),
        }

    def attribute_binding(self) -> dict:
        return self._req_engine.attribute_binding()

    def decision_inputs_declaration(self) -> dict:
        return self._req_engine.decision_inputs_declaration()

    def validate_unit_values(self, units: list[dict], engine_attributes: Mapping) -> list[str]:
        return self._req_engine.validate_unit_values(units, engine_attributes)

    def _call(self, action: str, payload: Mapping | None = None, *, hash_seed: str | None = None,
              id_salt: str | None = None) -> dict:
        res = BWORK.run_in_worker(
            self._files,
            kind=self.kind,
            action=action,
            config=self._config,
            payload=payload,
            unsandboxed_ok=self._unsandboxed_ok,
            limits=self._limits,
            hash_seed=self._hash_seed if hash_seed is None else str(hash_seed),
            id_salt=self._id_salt if id_salt is None else str(id_salt),
            _force_sandbox=self._force_sandbox,
        )
        if res.get("unsandboxed"):
            self.used_unsandboxed = True
            if self.run_dir:
                BWORK.record_unsandboxed(self.run_dir, action=action, engine_id=self.id)
        return res

    def _introspect(self) -> None:
        if self._card_cache is None or self._params_cache is None or self._has_batch_cache is None:
            info = self._call("introspect")["result"]
            d = info["card"]
            self._card_cache = EngineCard(**{k: d[k] for k in EngineCard.__dataclass_fields__ if k in d})
            self._params_cache = [ParamSpec(**p) for p in info["parameters"]]
            self._has_batch_cache = bool(info["has_batch"])

    def has_batch(self) -> bool:
        self._introspect()
        return bool(self._has_batch_cache)

    def describe(self) -> EngineCard:
        self._introspect()
        assert self._card_cache is not None
        return self._card_cache

    def parameters(self) -> list[ParamSpec]:
        self._introspect()
        assert self._params_cache is not None
        return list(self._params_cache)

    def _item_payload(self, params: dict, unit: UnitSpec, package: Package, horizon: Horizon,
                      seed: int, age_shares: Mapping | None = None) -> dict:
        p = {
            "params": dict(params or {}),
            "unit": {"id": unit.id, "attributes": dict(unit.attributes or {})},
            "package": {"id": package.id, "components": dict(package.components or {})},
            "horizon": {
                "start": float(horizon.start),
                "stop": float(horizon.stop),
                "window": list(horizon.window),
                "windows": {str(k): list(v) for k, v in (horizon.windows or {}).items()},
            },
            "seed": int(seed),
        }
        if age_shares:
            p["age_shares"] = dict(age_shares)
        return p

    def run(self, params: dict, unit: UnitSpec, package: Package, horizon: Horizon, seed: int,
            *, hash_seed: str | None = None, id_salt: str | None = None,
            age_shares: Mapping | None = None) -> Outcomes:
        r = self._call(
            "run",
            payload=self._item_payload(params, unit, package, horizon, seed, age_shares=age_shares),
            hash_seed=hash_seed,
            id_salt=id_salt,
        )["result"]
        return Outcomes(per_capita=r["per_capita"], windows=r["windows"],
                        cost_drivers=r["cost_drivers"], meta=r["meta"])

    def run_batch(self, param_sets: list[dict], unit: UnitSpec, packages: list[Package],
                  horizon: Horizon, seed: int, *, hash_seed: str | None = None,
                  id_salt: str | None = None, age_shares: Mapping | None = None) -> list[Outcomes]:
        items = [
            self._item_payload(prm, unit, pkg, horizon, seed, age_shares=age_shares)
            for prm, pkg in zip(param_sets, packages)
        ]
        res_list = self._call("run_batch", payload={"items": items}, hash_seed=hash_seed, id_salt=id_salt)["result"]
        return [
            Outcomes(per_capita=r["per_capita"], windows=r["windows"],
                     cost_drivers=r["cost_drivers"], meta=r["meta"])
            for r in res_list
        ]

    def run_many(self, cases: list[tuple[dict, UnitSpec, Package, Horizon, int]], *,
                 hash_seed: str | None = None, id_salt: str | None = None) -> list[Outcomes]:
        """Run heterogeneous `(params, unit, package, horizon, seed)` tuples sequentially in ONE worker
        process (preserving per-item unit/horizon/seed while detecting cross-call state/order leaks)."""
        items = [self._item_payload(prm, u, pkg, hor, s) for prm, u, pkg, hor, s in cases]
        res_list = self._call("run_many", payload={"items": items}, hash_seed=hash_seed, id_salt=id_salt)["result"]
        return [
            Outcomes(per_capita=r["per_capita"], windows=r["windows"],
                     cost_drivers=r["cost_drivers"], meta=r["meta"])
            for r in res_list
        ]


def _self_test() -> int:
    from toolkit import import_guard as G
    from toolkit.engines.identity import engine_identity
    from toolkit.engines.built import fixtures as BFIX
    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # Candidate module is refused by import_guard under every policy
    refused = True
    for kind in ("engine", "calibrator", "oracle"):
        try:
            G.module(kind, "toolkit.engines.built.candidate")
            refused = False
        except G.ImportRefused:
            pass
    b = BFIX.two_group_sir_build()
    files = BFIX.two_group_sir_files()
    cand = CandidateEngine(files, b, unsandboxed_ok=(BWORK.engine_sandbox_kind() is None))
    ident = engine_identity(cand)
    ok(refused and cand.ENGINE_REF == "toolkit.engines.built:BuiltEngine"
       and len(ident["code_sha256"]) == 64 and ident["request_sha256"] == BREQ.request_sha256(b),
       f"CAND1 CandidateEngine is blocked by import_guard and reports BuiltEngine identity ({ident['code_sha256'][:12]})")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
