#!/usr/bin/env python3
"""BuiltEngine production adapter (docs/design/agent-built-engines.md §5.1).

    python3 toolkit/engines/built/adapter.py --self-test

`BuiltEngine` is the sole Engine-contract class analysis uses for an agent-built engine. It is
constructible only from a run directory whose signed spec contains `engine.build` (verified against
the spec/brief sign-off record), whose run sign-off mode is `owner` or `lead`, whose
`engine_build/admission.json` passed admission, whose `engine_build/signoff.json` binds the
admission, snapshot, and `bench/` digests under an allowed signer for the run's sign-off mode,
and whose content-addressed snapshot verifies byte-for-byte before any compile. Nothing here names
a disease.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Mapping

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for _p in (ROOT, os.path.join(ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib import safe_io as SIO  # noqa: E402
from toolkit.engines.contract import EngineCard, Horizon, Outcomes, Package, ParamSpec, UnitSpec  # noqa: E402
from toolkit.engines.built import requested as BREQ  # noqa: E402
from toolkit.engines.built import snapshot as BSNAP  # noqa: E402
from toolkit.engines.built import worker as BWORK  # noqa: E402

DEP_MANIFESTS = ("requirements.txt", "requirements-agent.txt", "requirements-cloud.txt")
BENCH_FILES = ("bench/pack.yaml", "bench/results.yaml", "bench/benchmark_coverage.yaml")
OPTIONAL_BENCH_FILES = ("bench/fitted.yaml",)
ALLOWED_RUN_MODES = ("owner", "lead")


class BuiltEngineRefused(ValueError):
    """Raised when BuiltEngine construction or signed-artifact verification fails."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def effective_config_sha256(config: Mapping | None) -> str:
    return hashlib.sha256(json.dumps(dict(config or {}), sort_keys=True, default=str).encode("utf-8")).hexdigest()


def deps_sha256() -> str:
    """Digest over the three repo dependency manifests (as scripts/compute.py FP_DEP_MANIFESTS)."""
    parts = {}
    for m in DEP_MANIFESTS:
        p = os.path.join(ROOT, m)
        if os.path.isfile(p):
            with open(p, "rb") as f:
                parts[m] = hashlib.sha256(f.read()).hexdigest()
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode("utf-8")).hexdigest()


def starsim_version() -> str:
    from importlib import metadata
    try:
        return metadata.version("starsim")
    except metadata.PackageNotFoundError:
        return "not_installed"


def _read_single_link(run_dir: str, rel: str) -> bytes:
    try:
        data, st = SIO.read_nofollow(run_dir, rel)
    except (FileNotFoundError, SIO.LinkRefused, OSError) as e:
        raise BuiltEngineRefused(f"{rel} unreadable: {e}") from None
    if st.st_nlink > 1:
        raise BuiltEngineRefused(f"{rel} has multiple hard links (st_nlink={st.st_nlink})")
    return data


def _run_signoff_mode(run_dir: str) -> str | None:
    try:
        data = _read_single_link(run_dir, "run_state.json")
        doc = json.loads(data.decode("utf-8"))
        return str(doc.get("signoff")) if isinstance(doc, dict) and doc.get("signoff") is not None else None
    except (BuiltEngineRefused, UnicodeDecodeError, ValueError):
        return None


def _signer_allowed(by: str | None, mode: str) -> bool:
    if mode == "owner":
        return by == "owner"
    if mode == "lead":
        return by in ("owner", "lead")
    return False


def _verify_spec_signoff(run_dir: str, spec_sha: str, mode: str) -> dict:
    """Verify `decision_spec.yaml` is signed by an allowed signer for `mode` and unchanged since sign-off."""
    rec = None
    cand = "decision_spec.signoff.json"
    if os.path.lexists(os.path.join(run_dir, cand)):
        raw = _read_single_link(run_dir, cand)
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise BuiltEngineRefused(f"{cand} invalid JSON: {e}") from None
        if isinstance(doc, dict) and (doc.get("sha256") or {}).get("decision_spec.yaml") is not None:
            rec = doc
    if rec is None:
        raise BuiltEngineRefused("decision_spec.yaml has no valid sign-off record")
    if (rec.get("sha256") or {}).get("decision_spec.yaml") != spec_sha:
        raise BuiltEngineRefused("decision_spec.yaml sha256 does not match its sign-off record")
    sby = rec.get("signed_by")
    if not _signer_allowed(sby, mode):
        raise BuiltEngineRefused(f"spec sign-off signer {sby!r} is not allowed in signoff mode {mode!r}")
    return rec


def _compute_code_sha256(verified_files: Mapping[str, bytes]) -> str:
    """Path-independent code fingerprint computed from verified in-memory snapshot bytes plus
    the built-engine harness files (`contract.py`, `identity.py`, `toolkit/engines/built/*.py`)."""
    h = hashlib.sha256()
    for name in sorted(verified_files):
        h.update(f"snapshot/{name}".encode("utf-8") + b"\0")
        h.update(verified_files[name])
    built_dir = os.path.dirname(os.path.abspath(__file__))
    harness_entries: list[tuple[str, str]] = [
        ("toolkit/engines/contract.py", os.path.join(ROOT, "toolkit", "engines", "contract.py")),
        ("toolkit/engines/identity.py", os.path.join(ROOT, "toolkit", "engines", "identity.py")),
    ]
    for fname in sorted(f for f in os.listdir(built_dir) if f.endswith(".py")):
        harness_entries.append((f"toolkit/engines/built/{fname}", os.path.join(built_dir, fname)))
    for tag, fpath in harness_entries:
        h.update(tag.encode("utf-8") + b"\0")
        with open(fpath, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()


class BuiltEngine:
    """Sandboxed Engine-contract adapter for an admitted, signed agent-built engine snapshot."""

    ENGINE_REF = "toolkit.engines.built:BuiltEngine"
    batch_equals_run = True

    def __init__(self, *, run_dir: str, _unsandboxed_ok: bool = False, **config):
        import yaml
        if not isinstance(run_dir, str) or not os.path.isdir(run_dir):
            raise BuiltEngineRefused("BuiltEngine requires a valid run_dir passed by the harness")
        bad_kw = sorted(k for k in config if k in BREQ.RESERVED_CONFIG_KEYS or str(k).startswith("_"))
        if bad_kw:
            raise BuiltEngineRefused(f"reserved harness keywords refused in BuiltEngine config: {bad_kw}")
        self.run_dir = os.path.abspath(run_dir)
        self._unsandboxed_ok = bool(_unsandboxed_ok)
        self._force_sandbox: str | None = "auto"
        self._config = dict(config)
        self._config_sha = effective_config_sha256(self._config)

        # 1. Verify run sign-off mode (only 'owner' or 'lead' permitted for a signed built engine)
        mode = _run_signoff_mode(self.run_dir)
        if mode not in ALLOWED_RUN_MODES:
            raise BuiltEngineRefused(f"BuiltEngine refused in run with signoff mode {mode!r} "
                                     f"(requires one of {list(ALLOWED_RUN_MODES)})")

        # 2. Read decision_spec.yaml ONCE, verify spec sign-off and engine.build declaration
        spec_bytes = _read_single_link(self.run_dir, "decision_spec.yaml")
        spec_sha = _sha256_bytes(spec_bytes)
        _verify_spec_signoff(self.run_dir, spec_sha, mode)
        try:
            spec_doc = yaml.safe_load(spec_bytes.decode("utf-8")) or {}
        except (UnicodeDecodeError, yaml.YAMLError) as e:
            raise BuiltEngineRefused(f"decision_spec.yaml unreadable: {e}") from None
        eng_decl = spec_doc.get("engine") if isinstance(spec_doc, Mapping) else None
        if not (isinstance(eng_decl, Mapping) and isinstance(eng_decl.get("build"), Mapping)):
            raise BuiltEngineRefused("BuiltEngine requires a signed spec declaring engine.build")
        self._build = dict(eng_decl["build"])
        self._req_engine = BREQ.RequestedEngine(self._build)
        self._request_sha = BREQ.request_sha256(self._build)
        self.id = self._req_engine.id
        self.kind = self._req_engine.kind

        # 3-6. Verify signoff, admission, bench digests, and content-addressed snapshot via unified verifier
        allowed = ("owner",) if mode == "owner" else ("owner", "lead")
        info, probs = BSNAP.verify_admitted_engine(
            self.run_dir,
            require_signoff=True,
            request_sha256=self._request_sha,
            engine_id=self.id,
            config_sha256=self._config_sha,
            allowed_signers=allowed,
        )
        if probs:
            raise BuiltEngineRefused(probs[0])
        self._signoff = info["signoff"]
        self._admission = info["adm"]
        self._signed_bench_digests = dict(info["signed_bench_digests"])
        self.fitted_path = (
            os.path.join(self.run_dir, "engine_build", "bench", "fitted.yaml")
            if "bench/fitted.yaml" in self._signed_bench_digests
            else None
        )
        self.snapshot_sha256 = info["snapshot_sha256"]
        self._snapshots_dir = os.path.join(self.run_dir, "engine_build", "snapshots")
        self._files = info["verified_files"]
        self._code_sha256 = _compute_code_sha256(self._files)

        # Authoritative single bench/ set (§5.1)
        self.results_path = os.path.join(self.run_dir, "engine_build", "bench", "results.yaml")
        self.pack_path = os.path.join(self.run_dir, "engine_build", "bench", "pack.yaml")
        self.coverage_path = os.path.join(self.run_dir, "engine_build", "bench", "benchmark_coverage.yaml")
        self._limits: dict | None = None
        self._hash_seed: str = "0"
        self._id_salt: str = "s0"
        self._use_session: bool = True
        self.used_unsandboxed = False
        self._card_cache: EngineCard | None = None
        self._params_cache: list[ParamSpec] | None = None

    @classmethod
    def from_verified(
        cls,
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
        use_session: bool = False,
    ) -> "BuiltEngine":
        """Construct an unadmitted BuiltEngine wrapper directly from verified snapshot bytes (admission & try_tool)."""
        self = object.__new__(cls)
        self._direct_runner = True
        self._files = {str(k): bytes(v) for k, v in verified_files.items()}
        self._build = dict(build_decl)
        self._req_engine = BREQ.RequestedEngine(self._build)
        self._request_sha = BREQ.request_sha256(self._build)
        self.id = self._req_engine.id
        self.kind = self._req_engine.kind
        self._config = dict(config or {})
        self._config_sha = effective_config_sha256(self._config)
        self._code_sha256 = _compute_code_sha256(self._files)
        self.run_dir = os.path.abspath(run_dir) if run_dir else None
        self._unsandboxed_ok = bool(unsandboxed_ok)
        self._limits = dict(limits) if limits else None
        self._hash_seed = str(hash_seed)
        self._id_salt = str(id_salt)
        self._force_sandbox = "auto"
        self._use_session = bool(use_session)
        self.used_unsandboxed = False
        self.fitted_path = fitted_path
        self.results_path = results_path
        self.pack_path = pack_path
        self.coverage_path = coverage_path
        self._signed_bench_digests = {}
        self._card_cache = None
        self._params_cache = None
        return self

    def config(self) -> dict:
        return dict(self._config)

    def code_fingerprint(self) -> str:
        """Cached path-independent code sha256 computed from the verified in-memory snapshot bytes."""
        return self._code_sha256

    def identity_extra(self) -> dict:
        """Additional identity fields for toolkit.engines.identity.engine_identity (§5.1, BE23).
        Intentionally omits admission_sha256 to avoid circular dependency with stamped bench/results.yaml."""
        return {
            "request_sha256": self._request_sha,
            "effective_config_sha256": self._config_sha,
            "starsim_version": starsim_version(),
            "deps_sha256": deps_sha256(),
        }

    def read_verified_bench(self, rel: str) -> bytes:
        """Read `engine_build/<rel>` once (no-follow, single-link) and verify its sha256 against the
        signed digest recorded at admission/sign-off (prevents post-construction disk tampering)."""
        want = self._signed_bench_digests.get(rel)
        if not want:
            raise BuiltEngineRefused(f"{rel} is not a signed benchmark artifact of this built engine")
        raw = _read_single_link(self.run_dir, f"engine_build/{rel}")
        if _sha256_bytes(raw) != want:
            raise BuiltEngineRefused(f"engine_build/{rel} digest mismatch (tampered after sign-off)")
        return raw

    def attribute_binding(self) -> dict:
        return self._req_engine.attribute_binding()

    def decision_inputs_declaration(self) -> dict:
        return self._req_engine.decision_inputs_declaration()

    def validate_unit_values(self, units: list[dict], engine_attributes: Mapping) -> list[str]:
        return self._req_engine.validate_unit_values(units, engine_attributes)

    def _call_worker(self, action: str, payload: Mapping | None = None, *, hash_seed: str | None = None,
                     id_salt: str | None = None, use_session: bool | None = None) -> dict:
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
            use_session=self._use_session if use_session is None else bool(use_session),
        )
        if res.get("unsandboxed"):
            self.used_unsandboxed = True
            if self.run_dir:
                BWORK.record_unsandboxed(self.run_dir, action=action, engine_id=self.id)
        return res

    def describe(self) -> EngineCard:
        if self._card_cache is None:
            res = self._call_worker("describe")
            d = res["result"]
            self._card_cache = EngineCard(**{k: d[k] for k in EngineCard.__dataclass_fields__ if k in d})
        return self._card_cache

    def parameters(self) -> list[ParamSpec]:
        if self._params_cache is None:
            res = self._call_worker("parameters")
            self._params_cache = [ParamSpec(**p) for p in res["result"]]
        return list(self._params_cache)

    def _item_payload(self, params: dict, unit: UnitSpec, package: Package, horizon: Horizon, seed: int,
                      age_shares: Mapping | None = None) -> dict:
        p = {
            "params": dict(params or {}),
            "unit": {"id": unit.id, "attributes": dict(unit.attributes or {})},
            "package": {"id": package.id, "components": dict(package.components or {})},
            "horizon": {"start": float(horizon.start), "stop": float(horizon.stop),
                        "window": list(horizon.window),
                        "windows": {str(k): list(v) for k, v in (horizon.windows or {}).items()}},
            "seed": int(seed),
        }
        if age_shares:
            p["age_shares"] = dict(age_shares)
        return p

    def run(self, params: dict, unit: UnitSpec, package: Package, horizon: Horizon, seed: int,
            *, hash_seed: str | None = None, id_salt: str | None = None,
            age_shares: Mapping | None = None) -> Outcomes:
        payload = self._item_payload(params, unit, package, horizon, seed, age_shares=age_shares)
        res = self._call_worker("run", payload=payload, hash_seed=hash_seed, id_salt=id_salt)
        r = res["result"]
        return Outcomes(per_capita=r["per_capita"], windows=r["windows"],
                        cost_drivers=r["cost_drivers"], meta=r["meta"])

    def run_batch(self, param_sets: list[dict], unit_or_units, packages,
                  horizon: Horizon, seed: int, *, hash_seed: str | None = None,
                  id_salt: str | None = None, age_shares: Mapping | None = None) -> list[Outcomes]:
        """Run a batch of `(params, unit, package)` evaluations inside chunked sandboxed workers (`run_many`).
        Accepts either a single `UnitSpec`/`Package` (broadcast across items) or per-item sequences."""
        n = len(param_sets)
        if n == 0:
            return []
        units = [unit_or_units] * n if isinstance(unit_or_units, UnitSpec) else list(unit_or_units)
        pkgs = [packages] * n if isinstance(packages, Package) else list(packages)
        if len(units) != n or len(pkgs) != n:
            raise ValueError(f"run_batch length mismatch: params={n}, units={len(units)}, packages={len(pkgs)}")
        chunk_sz = 2048 if self.kind == "closed_form" else 32
        out: list[Outcomes] = []
        for a in range(0, n, chunk_sz):
            items = [
                self._item_payload(p, u, pk, horizon, seed, age_shares=age_shares)
                for p, u, pk in zip(param_sets[a:a + chunk_sz], units[a:a + chunk_sz], pkgs[a:a + chunk_sz])
            ]
            res_list = self._call_worker("run_many", payload={"items": items},
                                         hash_seed=hash_seed, id_salt=id_salt)["result"]
            out.extend(
                Outcomes(per_capita=r["per_capita"], windows=r["windows"],
                         cost_drivers=r["cost_drivers"], meta=r["meta"])
                for r in res_list
            )
        return out

    def run_many(self, cases: list[tuple[dict, UnitSpec, Package, Horizon, int]], *,
                 hash_seed: str | None = None, id_salt: str | None = None) -> list[Outcomes]:
        """Run heterogeneous `(params, unit, package, horizon, seed)` tuples in chunked sandboxed worker calls."""
        if not cases:
            return []
        chunk_sz = 2048 if self.kind == "closed_form" else 32
        out: list[Outcomes] = []
        for a in range(0, len(cases), chunk_sz):
            items = [self._item_payload(prm, u, pkg, hor, s) for prm, u, pkg, hor, s in cases[a:a + chunk_sz]]
            res_list = self._call_worker("run_many", payload={"items": items},
                                         hash_seed=hash_seed, id_salt=id_salt)["result"]
            out.extend(
                Outcomes(per_capita=r["per_capita"], windows=r["windows"],
                         cost_drivers=r["cost_drivers"], meta=r["meta"])
                for r in res_list
            )
        return out


def _self_test_fixture(d: str, *, py_src: bytes, sign_by: str = "lead", spec_sign_by: str | None = None,
                       run_mode: str = "lead", config: dict | None = None, include_build: bool = True,
                       sign_spec: bool = True, bench_contents: Mapping[str, bytes] | None = None) -> dict:
    """Create a complete admitted & signed built-engine fixture run directory in `d`."""
    import yaml
    b = BREQ.example_build()
    b["kind"] = "closed_form"
    b["request"].pop("compartments", None)
    spec_doc = {"schema_version": 1, "engine": {"build": b} if include_build else {"ref": "toolkit.engines.contract:ToyEngine"}}
    spec_bytes = yaml.safe_dump(spec_doc, sort_keys=False).encode("utf-8")
    open(os.path.join(d, "decision_spec.yaml"), "wb").write(spec_bytes)
    if sign_spec:
        spec_so = {
            "signed_by": spec_sign_by or sign_by,
            "sha256": {"decision_spec.yaml": _sha256_bytes(spec_bytes)},
            "producer": "toolkit/spec/signoff.py",
        }
        open(os.path.join(d, "decision_spec.signoff.json"), "w").write(json.dumps(spec_so, indent=1, sort_keys=True))
    open(os.path.join(d, "run_state.json"), "w").write(json.dumps({"signoff": run_mode}))
    work = os.path.join(d, "models", "ext", f"engine_{b['id']}")
    os.makedirs(work, exist_ok=True)
    open(os.path.join(work, "engine.py"), "wb").write(py_src)
    open(os.path.join(work, "card.yaml"), "wb").write(b"name: built:two_stage_mod\nversion: '0.1'\nmechanisms: [m]\n")
    open(os.path.join(work, "ENGINE_NOTES.md"), "wb").write(b"# notes\n")
    snaps = os.path.join(d, "engine_build", "snapshots")
    man = BSNAP.create_snapshot(work, snaps)
    snap_sha = man["snapshot_sha256"]
    bench = os.path.join(d, "engine_build", "bench")
    os.makedirs(bench, exist_ok=True)
    defaults = {
        "bench/pack.yaml": b"benchmarks: []\n",
        "bench/results.yaml": b"benchmarks: []\n",
        "bench/benchmark_coverage.yaml": b"engine: built:two_stage_mod\nbenchmarks: {}\n",
    }
    if bench_contents:
        defaults.update(bench_contents)
    digests = {}
    for rel, content in defaults.items():
        open(os.path.join(d, "engine_build", rel), "wb").write(content)
        digests[rel] = _sha256_bytes(content)
    adm = {
        "status": "admitted",
        "engine_id": b["id"],
        "request_sha256": BREQ.request_sha256(b),
        "snapshot_sha256": snap_sha,
        "config_sha256": effective_config_sha256(config or {}),
        "checks": {"A1": {"status": "pass", "passed": 1, "total": 1}},
        "digests": digests,
    }
    adm_bytes = json.dumps(adm, indent=1, sort_keys=True).encode("utf-8")
    open(os.path.join(d, "engine_build", "admission.json"), "wb").write(adm_bytes)
    sheet_bytes = b"# Admission sheet\n"
    open(os.path.join(d, "engine_build", "admission_sheet.md"), "wb").write(sheet_bytes)
    so = {
        "by": sign_by,
        "snapshot_sha256": snap_sha,
        "sha256": {
            "admission.json": _sha256_bytes(adm_bytes),
            "admission_sheet.md": _sha256_bytes(sheet_bytes),
            "snapshot": snap_sha,
            **digests,
        },
    }
    open(os.path.join(d, "engine_build", "signoff.json"), "w").write(json.dumps(so, indent=1, sort_keys=True))
    return {"snapshot_sha256": snap_sha, "admission_sha256": _sha256_bytes(adm_bytes)}


def _self_test() -> int:
    import tempfile
    import yaml
    from toolkit.engines import benchmark as BM
    from toolkit.engines import benchmark_sources as BS
    from toolkit.engines.identity import engine_identity
    from toolkit.spec import schema

    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    dev_unsandboxed = BWORK.engine_sandbox_kind() is None

    def _mk_py(ver_tag: str) -> bytes:
        return (
            "from toolkit.engines.contract import EngineCard, Outcomes, ParamSpec\n"
            f"TAG = {ver_tag!r}\n"
            "class Model:\n"
            "    def __init__(self, k=1.0):\n"
            "        self.k = k\n"
            "    def describe(self):\n"
            "        return EngineCard(name='built:two_stage_mod', version='0.1', mechanisms=['m'],\n"
            "                          components={'comp_a': [0, 1]}, unit_attributes={'N': 'n', 'rate_in': 'r'},\n"
            "                          outputs={'per_capita': ['cases', 'severe'], 'windows': ['prev'], 'cost_drivers': ['units_delivered']},\n"
            "                          age_groups=['all'], valid_ranges={'rate_in': [0.01, 0.5]})\n"
            "    def parameters(self):\n"
            "        return [ParamSpec('eff_a', 0.6, 'fitted', lo=0.2, hi=0.9, source='dossier:E1')]\n"
            "    def evaluate(self, params, unit, package, horizon):\n"
            "        v = float(unit.attributes.get('rate_in', 0.1)) * self.k\n"
            "        a = int(package.components.get('comp_a', 0))\n"
            "        eff = (1.0 - float(params['eff_a']) * a)\n"
            "        return Outcomes(per_capita={'cases': {'all': v * eff}, 'severe': {'all': 0.1 * v * eff}},\n"
            "                        windows={'w0': {'prev': v * eff}}, cost_drivers={'units_delivered': float(a)})\n"
        ).encode("utf-8")

    # BE23 + P1-18 + P2-1: path-independent, snapshot-distinct, omits admission_sha256, live instance immune to disk tamper
    with tempfile.TemporaryDirectory() as root_tmp:
        d1 = os.path.join(root_tmp, "aaa_run")
        d1_same = os.path.join(root_tmp, "zzz_run")
        d2 = os.path.join(root_tmp, "bbb_run")
        for p in (d1, d1_same, d2):
            os.makedirs(p)
        info1 = _self_test_fixture(d1, py_src=_mk_py("v1"))
        _self_test_fixture(d1_same, py_src=_mk_py("v1"))
        _self_test_fixture(d2, py_src=_mk_py("v2"))
        eng1 = BuiltEngine(run_dir=d1, _unsandboxed_ok=dev_unsandboxed)
        id1a = engine_identity(eng1)
        id1b = engine_identity(BuiltEngine(run_dir=d1_same, _unsandboxed_ok=dev_unsandboxed))
        id2 = engine_identity(BuiltEngine(run_dir=d2, _unsandboxed_ok=dev_unsandboxed))
        ok(id1a == id1b and id1a["code_sha256"] != id2["code_sha256"]
           and "admission_sha256" not in id1a and info1["admission_sha256"] not in json.dumps(id1a),
           "BE23 engine_identity is path-independent, differs across snapshots, and omits admission_sha256")

        # BE22: refusals (including P1-2 unsandboxed_ok in config, P1-4 unsigned spec & run_mode='none')
        refusals = {}
        try:
            BuiltEngine(run_dir=d1, _unsandboxed_ok=dev_unsandboxed, k=2.0)
            refusals["changed_kw"] = False
        except BuiltEngineRefused:
            refusals["changed_kw"] = True
        try:
            schema.load_engine({"ref": "toolkit.engines.built:BuiltEngine", "config": {"unsandboxed_ok": True}},
                               allow_built=True, run_dir=d1)
            refusals["choice_unsandboxed_ok"] = False
        except schema.SpecError:
            refusals["choice_unsandboxed_ok"] = True
        try:
            BuiltEngine(run_dir=d1, **{"unsandboxed_ok": True})
            refusals["ctor_unsandboxed_ok"] = False
        except BuiltEngineRefused:
            refusals["ctor_unsandboxed_ok"] = True
        with tempfile.TemporaryDirectory() as d_own:
            _self_test_fixture(d_own, py_src=_mk_py("v1"), sign_by="lead", spec_sign_by="owner", run_mode="owner")
            try:
                BuiltEngine(run_dir=d_own, _unsandboxed_ok=dev_unsandboxed)
                refusals["lead_in_owner"] = False
            except BuiltEngineRefused:
                refusals["lead_in_owner"] = True
        with tempfile.TemporaryDirectory() as d_none:
            _self_test_fixture(d_none, py_src=_mk_py("v1"), sign_by="owner", run_mode="none")
            try:
                BuiltEngine(run_dir=d_none, _unsandboxed_ok=dev_unsandboxed)
                refusals["mode_none"] = False
            except BuiltEngineRefused:
                refusals["mode_none"] = True
        with tempfile.TemporaryDirectory() as d_uns:
            _self_test_fixture(d_uns, py_src=_mk_py("v1"), sign_spec=False)
            try:
                BuiltEngine(run_dir=d_uns, _unsandboxed_ok=dev_unsandboxed)
                refusals["unsigned_spec"] = False
            except BuiltEngineRefused:
                refusals["unsigned_spec"] = True
        with tempfile.TemporaryDirectory() as d_nb:
            _self_test_fixture(d_nb, py_src=_mk_py("v1"), include_build=False)
            try:
                BuiltEngine(run_dir=d_nb, _unsandboxed_ok=dev_unsandboxed)
                refusals["no_build_req"] = False
            except BuiltEngineRefused:
                refusals["no_build_req"] = True
        with tempfile.TemporaryDirectory() as d_res:
            _self_test_fixture(d_res, py_src=_mk_py("v1"))
            open(os.path.join(d_res, "engine_build", "bench", "results.yaml"), "a").write("# edited\n")
            try:
                BuiltEngine(run_dir=d_res, _unsandboxed_ok=dev_unsandboxed)
                refusals["tampered_results"] = False
            except BuiltEngineRefused:
                refusals["tampered_results"] = True
        with tempfile.TemporaryDirectory() as d_adm:
            _self_test_fixture(d_adm, py_src=_mk_py("v1"))
            open(os.path.join(d_adm, "engine_build", "admission.json"), "a").write(" ")
            try:
                BuiltEngine(run_dir=d_adm, _unsandboxed_ok=dev_unsandboxed)
                refusals["tampered_admission"] = False
            except BuiltEngineRefused:
                refusals["tampered_admission"] = True
        refusals["direct_schema"] = bool(schema.engine_ref_problems({"engine": {"ref": "toolkit.engines.built:BuiltEngine"}}))
        ok(all(refusals.values()),
           f"BE22 BuiltEngine refuses invalid config/unsandboxed_ok, unsigned spec, run_mode=none, tampered artifacts, "
           f"lead sig in owner run, and direct schema ref ({refusals})")

        # P1-3: unsandboxed execution writes durable marker that survives discarding the adapter
        with tempfile.TemporaryDirectory() as d_un:
            _self_test_fixture(d_un, py_src=_mk_py("v1"))
            e_un = BuiltEngine(run_dir=d_un, _unsandboxed_ok=True)
            e_un._force_sandbox = None
            e_un.run({}, UnitSpec("u1", {"N": 1000, "rate_in": 0.1}), Package("p1", {"comp_a": 1}),
                     Horizon(0, 1, (0, 1), {"w0": [0, 1]}), 1)
            del e_un
            u_finds = BWORK.unsandboxed_findings(d_un)
            ok(len(u_finds) == 1 and u_finds[0]["kind"] == "engine_unsandboxed"
               and u_finds[0]["severity"] == "HIGH" and u_finds[0]["scope_declarable"] is False,
               f"BE35b unsandboxed built-engine run persists durable HIGH non-declarable gate finding ({u_finds})")

        # P1-6 + P1-7 + P1-8: verified_results with public ref + fitted parameter + post-construction tamper refusal
        with tempfile.TemporaryDirectory() as d_bm:
            pack_doc = {
                "engine": "toolkit.engines.built:BuiltEngine",
                "seeds": [1],
                "initialisation": {"method": "closed form", "evidence": ["not applicable: closed form"]},
                "horizon": {"start": 0, "stop": 1, "window": [0, 1], "windows": {"w0": [0, 1]}},
                "settings": {"s": {"unit": {"N": 1000, "rate_in": 0.1}}},
                "arms": {"none": {"setting": "s", "package": {"comp_a": 0}},
                         "a": {"setting": "s", "package": {"comp_a": 1}}},
                "benchmarks": [{
                    "id": "T1", "metric": "a vs none", "target": 0.4, "band": [0.3, 0.5],
                    "published": {"lower": 0.3, "upper": 0.5, "level": 0.95, "scale": "ratio", "table": "T1"},
                    "estimand_type": "risk_ratio", "diagnostic": "mixed", "age_band": "mixed",
                    "estimand": {"type": "ratio", "num": "a", "den": "none", "value": "per_capita.cases.all"},
                }],
                "fit": [{"param": "eff_a", "grid": [0.4, 0.6], "targets": ["T1"]}],
            }
            pack_bytes = yaml.safe_dump(pack_doc, sort_keys=False).encode("utf-8")
            _self_test_fixture(d_bm, py_src=_mk_py("v1"), bench_contents={"bench/pack.yaml": pack_bytes})
            e_probe = BuiltEngine(run_dir=d_bm, _unsandboxed_ok=dev_unsandboxed)
            fitted_doc = BM.stamp({
                "parameters": {"eff_a": {"value": 0.6, "lo": 0.2, "hi": 0.9}},
                "band_declarations": BS.fit_declarations(pack_doc),
            }, e_probe)
            rows = [{
                "id": "T1", "metric": "a vs none", "role": "fit", "computed": 0.4,
                "band": [0.3, 0.5], "status": "in_band", "in_band": True,
                "band_sha256": BS.band_digest(pack_doc["benchmarks"][0], pack_doc),
            }]
            res_doc = BM.stamp({"benchmarks": rows}, e_probe)
            _self_test_fixture(
                d_bm,
                py_src=_mk_py("v1"),
                bench_contents={
                    "bench/pack.yaml": pack_bytes,
                    "bench/results.yaml": yaml.safe_dump(res_doc, sort_keys=False).encode("utf-8"),
                    "bench/fitted.yaml": yaml.safe_dump(fitted_doc, sort_keys=False).encode("utf-8"),
                },
            )
            e_bm = BuiltEngine(run_dir=d_bm, _unsandboxed_ok=dev_unsandboxed)
            v_doc, v_why = BM.verified_results(e_bm)
            # Tamper ONLY `computed` on disk AFTER construction
            tampered = dict(res_doc, benchmarks=[dict(rows[0], computed=0.45)])
            open(e_bm.results_path, "wb").write(yaml.safe_dump(tampered, sort_keys=False).encode("utf-8"))
            t_doc, t_why = BM.verified_results(e_bm)
            ok(v_doc is not None and v_why is None and t_doc is None and "tampered" in str(t_why),
               f"BE23b verified_results accepts public BuiltEngine ref + fitted.yaml and refuses post-construction edit "
               f"(ok={v_why}, tampered={t_why})")

        # BE_BATCH1: run_batch matches run pair-by-pair for heterogeneous (params, unit, package) inputs
        u_b0 = UnitSpec("b0", {"N": 1000.0, "rate_in": 0.15})
        u_b1 = UnitSpec("b1", {"N": 2000.0, "rate_in": 0.30})
        p_b0, p_b1 = Package("p0", {"comp_a": 0}), Package("p1", {"comp_a": 1})
        h_b = Horizon(0.0, 1.0, (0.0, 1.0), {"w0": [0.0, 1.0]})
        rb_outs = eng1.run_batch([{"eff_a": 0.3}, {"eff_a": 0.6}], [u_b0, u_b1], [p_b0, p_b1], h_b, 7)
        s0 = eng1.run({"eff_a": 0.3}, u_b0, p_b0, h_b, 7)
        s1 = eng1.run({"eff_a": 0.6}, u_b1, p_b1, h_b, 7)
        ok(eng1.batch_equals_run is True and len(rb_outs) == 2
           and rb_outs[0].per_capita == s0.per_capita and rb_outs[1].per_capita == s1.per_capita
           and rb_outs[0].cost_drivers == s0.cost_drivers and rb_outs[1].cost_drivers == s1.cost_drivers,
           "BE_BATCH1 BuiltEngine.run_batch matches run() pair-by-pair on heterogeneous units/packages")

        # BE19e + P1-18: tampered snapshot file refused on new construction, while live adapter keeps cached code_sha256
        sdir = os.path.join(d1, "engine_build", "snapshots", info1["snapshot_sha256"])
        os.chmod(sdir, 0o755)
        ep = os.path.join(sdir, "engine.py")
        os.chmod(ep, 0o644)
        open(ep, "ab").write(b"\n# edited\n")
        live_same = engine_identity(eng1)["code_sha256"] == id1a["code_sha256"]
        try:
            BuiltEngine(run_dir=d1, _unsandboxed_ok=dev_unsandboxed)
            snap_refused = False
        except BuiltEngineRefused:
            snap_refused = True
        ok(live_same and snap_refused and bool(BSNAP.verify_engine_snapshot(d1)),
           "BE19e live adapter fingerprints cached verified bytes; new BuiltEngine and verify_engine_snapshot refuse tamper")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
