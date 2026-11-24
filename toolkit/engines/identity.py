#!/usr/bin/env python3
"""Engine code fingerprinting and identity (delegating to contract.py for shipped engines).

    python3 toolkit/engines/identity.py --self-test

Shipped engines keep their exact `toolkit.engines.contract` fingerprints without modifying
`contract.py` (whose bytes are hashed into every shipped engine's stamped `results.yaml`). Engines
that implement `code_fingerprint()` and/or `identity_extra()` (specifically `BuiltEngine`) report an
installation-path-independent fingerprint computed directly from their verified in-memory snapshot
bytes plus harness files, plus their request, effective-config, starsim, and dependency digests.
Nothing here names a disease.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from toolkit.engines import contract as _C  # noqa: E402


def code_fingerprint(engine) -> str:
    """sha256 over the engine's code. Uses `engine.code_fingerprint()` when defined (e.g. BuiltEngine,
    hashed from verified in-memory snapshot bytes + harness files in canonical order), else delegates
    directly to `toolkit.engines.contract.code_fingerprint(engine)`."""
    custom = getattr(engine, "code_fingerprint", None)
    if callable(custom):
        return str(custom())
    return _C.code_fingerprint(engine)


def engine_identity(engine) -> dict:
    """Name, version, code fingerprint, parameter digest, structure digest, plus any `identity_extra()`
    fields declared by the engine. For engines without custom hooks, delegates directly to
    `toolkit.engines.contract.engine_identity(engine)`."""
    has_custom_fp = callable(getattr(engine, "code_fingerprint", None))
    has_extra = callable(getattr(engine, "identity_extra", None))
    if not (has_custom_fp or has_extra):
        return _C.engine_identity(engine)
    card = engine.describe()
    params = sorted((p.name, float(p.value), p.kind, p.lo, p.hi) for p in engine.parameters())
    struct = {k: getattr(card, k) for k in ("components", "unit_attributes", "outputs", "age_groups", "valid_ranges")}
    h = lambda x: hashlib.sha256(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()  # noqa: E731
    out = {
        "name": card.name,
        "version": card.version,
        "code_sha256": code_fingerprint(engine),
        "params_sha256": h(params),
        "config_sha256": h(struct),
    }
    if has_extra:
        extra = engine.identity_extra()
        if isinstance(extra, dict):
            out.update(extra)
    return out


def _self_test() -> int:
    import yaml
    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # ID1: shipped engines keep their exact stamped results.yaml code_sha256 (contract.py untouched)
    from toolkit.engines.compartmental.engine import CompartmentalEngine
    from toolkit.engines.malaria.engine import MalariaEngine
    from toolkit.engines.renewal.engine import RenewalEngine
    shipped = [
        (MalariaEngine, "toolkit/engines/malaria/benchmarks/results.yaml"),
        (CompartmentalEngine, "toolkit/engines/compartmental/benchmarks/results.yaml"),
        (RenewalEngine, "toolkit/engines/renewal/benchmarks/results.yaml"),
    ]
    for cls, res_rel in shipped:
        stamp = (yaml.safe_load(open(os.path.join(ROOT, res_rel))) or {})["engine_identity"]["code_sha256"]
        inst = cls.__new__(cls)
        fp_id = code_fingerprint(inst)
        fp_c = _C.code_fingerprint(inst)
        ok(fp_id == fp_c == stamp, f"ID1 {cls.__name__} fingerprint matches stamped results.yaml ({fp_id[:12]})")

    # ID2: custom engine hooks (code_fingerprint + identity_extra) are used without affecting ToyEngine
    toy = _C.ToyEngine()
    ok(engine_identity(toy) == _C.engine_identity(toy), "ID2 ToyEngine delegates identically to contract.engine_identity")

    class CustomToy(_C.ToyEngine):
        def code_fingerprint(self) -> str:
            return "a" * 64

        def identity_extra(self) -> dict:
            return {"request_sha256": "b" * 64}

    cid = engine_identity(CustomToy())
    ok(cid["code_sha256"] == "a" * 64 and cid["request_sha256"] == "b" * 64 and cid["name"] == "toy",
       "ID3 custom code_fingerprint() and identity_extra() flow through engine_identity()")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
