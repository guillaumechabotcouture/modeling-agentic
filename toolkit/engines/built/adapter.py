#!/usr/bin/env python3
"""BuiltEngine production adapter (docs/design/agent-built-engines.md §5.1).

    python3 toolkit/engines/built/adapter.py --self-test

`BuiltEngine` is the sole Engine-contract class analysis uses for an agent-built engine. It is
constructible only from a run directory whose signed spec contains `engine.build`, whose
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


class BuiltEngineRefused(ValueError):
    """Raised when BuiltEngine construction fails any provenance, signature, or snapshot check."""


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


class BuiltEngine:
    """Sandboxed Engine-contract adapter for an admitted, signed agent-built engine snapshot."""

    def __init__(self, *, run_dir: str, unsandboxed_ok: bool = False, **config):
        import yaml
        if not isinstance(run_dir, str) or not os.path.isdir(run_dir):
            raise BuiltEngineRefused("BuiltEngine requires a valid run_dir passed by the harness")
        bad_kw = sorted(k for k in BREQ.RESERVED_CONFIG_KEYS if k in config)
        if bad_kw:
            raise BuiltEngineRefused(f"reserved harness keywords refused in BuiltEngine config: {bad_kw}")
        self.run_dir = os.path.abspath(run_dir)
        self.unsandboxed_ok = bool(unsandboxed_ok)
        self._config = dict(config)
        self._config_sha = effective_config_sha256(self._config)

        # 1. Read signed spec and verify it declares engine.build
        spec_bytes = _read_single_link(self.run_dir, "decision_spec.yaml")
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

        # 2. Read engine_build/signoff.json and verify signer against the run's recorded sign-off mode
        so_bytes = _read_single_link(self.run_dir, "engine_build/signoff.json")
        try:
            so = json.loads(so_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise BuiltEngineRefused(f"engine_build/signoff.json invalid JSON: {e}") from None
        if not isinstance(so, dict) or not isinstance(so.get("sha256"), dict):
            raise BuiltEngineRefused("engine_build/signoff.json malformed")
        by = so.get("by")
        mode = _run_signoff_mode(self.run_dir)
        if by == "owner":
            pass
        elif by == "lead":
            if mode != "lead":
                raise BuiltEngineRefused(f"lead engine sign-off is refused in a run with signoff mode {mode!r}")
        else:
            raise BuiltEngineRefused(f"engine_build/signoff.json has unrecognized signer {by!r}")
        self._signoff = so

        # 3. Read engine_build/admission.json, verify sha256 against signoff and status == admitted
        adm_bytes = _read_single_link(self.run_dir, "engine_build/admission.json")
        adm_sha = _sha256_bytes(adm_bytes)
        if so["sha256"].get("admission.json") != adm_sha:
            raise BuiltEngineRefused("engine_build/admission.json sha256 does not match engine_build/signoff.json")
        try:
            adm = json.loads(adm_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise BuiltEngineRefused(f"engine_build/admission.json invalid JSON: {e}") from None
        if not isinstance(adm, dict) or adm.get("status") != "admitted":
            raise BuiltEngineRefused(f"built engine is not admitted (status={adm.get('status') if isinstance(adm, dict) else None!r})")
        if adm.get("engine_id") != self.id:
            raise BuiltEngineRefused(f"admission engine_id {adm.get('engine_id')!r} != spec engine.build.id {self.id!r}")
        if adm.get("request_sha256") != self._request_sha:
            raise BuiltEngineRefused("admission request_sha256 does not match signed decision_spec.yaml engine.build")
        self._admission = adm

        # Verify bench/* digests against BOTH admission.json and signoff.json
        adm_digests = adm.get("digests") if isinstance(adm.get("digests"), Mapping) else {}
        for rel in BENCH_FILES:
            want_so = so["sha256"].get(rel)
            want_adm = adm_digests.get(rel)
            if not want_so or not want_adm or want_so != want_adm:
                raise BuiltEngineRefused(f"{rel} digest missing or inconsistent between admission.json and signoff.json")
            got = _sha256_bytes(_read_single_link(self.run_dir, f"engine_build/{rel}"))
            if got != want_so:
                raise BuiltEngineRefused(f"engine_build/{rel} digest mismatch (tampered after admission/signoff)")

        # 4. Verify snapshot_sha256 and effective config_sha256 match admitted record
        snap_sha = adm.get("snapshot_sha256")
        if not isinstance(snap_sha, str) or so["sha256"].get("snapshot") != snap_sha:
            raise BuiltEngineRefused("snapshot_sha256 missing or inconsistent between admission.json and signoff.json")
        if adm.get("config_sha256") != self._config_sha:
            raise BuiltEngineRefused(f"constructor config sha256 {self._config_sha[:12]} does not match admitted "
                                     f"{str(adm.get('config_sha256'))[:12]}")
        self.snapshot_sha256 = snap_sha
        self._snapshots_dir = os.path.join(self.run_dir, "engine_build", "snapshots")

        # 5. Verify snapshot bytes ONCE before any compile (BE19)
        try:
            self._files = BSNAP.read_verified_snapshot(self._snapshots_dir, self.snapshot_sha256)
        except BSNAP.SnapshotError as e:
            raise BuiltEngineRefused(f"snapshot verification failed: {e}") from None

        # Authoritative single bench/ set (§5.1)
        self.results_path = os.path.join(self.run_dir, "engine_build", "bench", "results.yaml")
        self.pack_path = os.path.join(self.run_dir, "engine_build", "bench", "pack.yaml")
        self.coverage_path = os.path.join(self.run_dir, "engine_build", "bench", "benchmark_coverage.yaml")
        self.used_unsandboxed = False
        self._card_cache: EngineCard | None = None
        self._params_cache: list[ParamSpec] | None = None

    def config(self) -> dict:
        return dict(self._config)

    def fingerprint_files(self) -> list[str]:
        """Files covered by contract.code_fingerprint(engine): snapshot files + toolkit/engines/built/*.py."""
        snap_dir = os.path.join(self._snapshots_dir, self.snapshot_sha256)
        snap_files = [os.path.join(snap_dir, name) for name in sorted(self._files)]
        built_dir = os.path.dirname(os.path.abspath(__file__))
        harness_files = sorted(
            os.path.join(built_dir, f) for f in os.listdir(built_dir) if f.endswith(".py")
        )
        return snap_files + harness_files

    def identity_extra(self) -> dict:
        """Additional identity fields for contract.engine_identity (§5.1, BE23).
        Intentionally omits admission_sha256 to avoid circular dependency with stamped bench/results.yaml."""
        return {
            "request_sha256": self._request_sha,
            "effective_config_sha256": self._config_sha,
            "starsim_version": starsim_version(),
            "deps_sha256": deps_sha256(),
        }

    def attribute_binding(self) -> dict:
        return self._req_engine.attribute_binding()

    def decision_inputs_declaration(self) -> dict:
        return self._req_engine.decision_inputs_declaration()

    def validate_unit_values(self, units: list[dict], engine_attributes: Mapping) -> list[str]:
        return self._req_engine.validate_unit_values(units, engine_attributes)

    def describe(self) -> EngineCard:
        if self._card_cache is None:
            res = BWORK.run_in_worker(self._files, kind=self.kind, action="describe",
                                      config=self._config, unsandboxed_ok=self.unsandboxed_ok)
            self.used_unsandboxed = self.used_unsandboxed or bool(res["unsandboxed"])
            d = res["result"]
            self._card_cache = EngineCard(**{k: d[k] for k in EngineCard.__dataclass_fields__ if k in d})
        return self._card_cache

    def parameters(self) -> list[ParamSpec]:
        if self._params_cache is None:
            res = BWORK.run_in_worker(self._files, kind=self.kind, action="parameters",
                                      config=self._config, unsandboxed_ok=self.unsandboxed_ok)
            self.used_unsandboxed = self.used_unsandboxed or bool(res["unsandboxed"])
            self._params_cache = [ParamSpec(**p) for p in res["result"]]
        return list(self._params_cache)

    def run(self, params: dict, unit: UnitSpec, package: Package, horizon: Horizon, seed: int) -> Outcomes:
        payload = {
            "params": dict(params or {}),
            "unit": {"id": unit.id, "attributes": dict(unit.attributes or {})},
            "package": {"id": package.id, "components": dict(package.components or {})},
            "horizon": {"start": float(horizon.start), "stop": float(horizon.stop),
                        "window": list(horizon.window),
                        "windows": {str(k): list(v) for k, v in (horizon.windows or {}).items()}},
            "seed": int(seed),
        }
        res = BWORK.run_in_worker(self._files, kind=self.kind, action="run", config=self._config,
                                  payload=payload, unsandboxed_ok=self.unsandboxed_ok)
        self.used_unsandboxed = self.used_unsandboxed or bool(res["unsandboxed"])
        r = res["result"]
        return Outcomes(per_capita=r["per_capita"], windows=r["windows"],
                        cost_drivers=r["cost_drivers"], meta=r["meta"])


def _self_test_fixture(d: str, *, py_src: bytes, sign_by: str = "lead", run_mode: str = "lead",
                       config: dict | None = None, include_build: bool = True) -> dict:
    """Create a complete admitted & signed built-engine fixture run directory in `d`."""
    import yaml
    b = BREQ.example_build()
    b["kind"] = "closed_form"
    b["request"].pop("compartments", None)
    spec_doc = {"schema_version": 1, "engine": {"build": b} if include_build else {"ref": "toolkit.engines.contract:ToyEngine"}}
    open(os.path.join(d, "decision_spec.yaml"), "w").write(yaml.safe_dump(spec_doc, sort_keys=False))
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
    digests = {}
    for rel, content in (("bench/pack.yaml", b"benchmarks: []\n"),
                         ("bench/results.yaml", b"benchmarks: []\n"),
                         ("bench/benchmark_coverage.yaml", b"engine: built:two_stage_mod\nbenchmarks: {}\n")):
        open(os.path.join(d, "engine_build", rel), "wb").write(content)
        digests[rel] = _sha256_bytes(content)
    adm = {
        "status": "admitted",
        "engine_id": b["id"],
        "request_sha256": BREQ.request_sha256(b),
        "snapshot_sha256": snap_sha,
        "config_sha256": effective_config_sha256(config or {}),
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
    from toolkit.engines.contract import engine_identity
    from toolkit.spec import schema

    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

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
            "        return Outcomes(per_capita={'cases': {'all': v}, 'severe': {'all': 0.1 * v}},\n"
            "                        windows={'w0': {'prev': v}}, cost_drivers={'units_delivered': float(package.components.get('comp_a', 0))})\n"
        ).encode("utf-8")

    # BE22 & BE19 & BE23
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        info1 = _self_test_fixture(d1, py_src=_mk_py("v1"))
        eng1 = BuiltEngine(run_dir=d1, unsandboxed_ok=True)
        id1a = engine_identity(eng1)
        id1b = engine_identity(BuiltEngine(run_dir=d1, unsandboxed_ok=True))
        _self_test_fixture(d2, py_src=_mk_py("v2"))
        id2 = engine_identity(BuiltEngine(run_dir=d2, unsandboxed_ok=True))
        ok(id1a == id1b and id1a["code_sha256"] != id2["code_sha256"]
           and "admission_sha256" not in id1a and info1["admission_sha256"] not in json.dumps(id1a),
           "BE23 engine_identity differs for different snapshots, matches for same snapshot, and omits admission_sha256")

        # BE22: refusals
        refusals = {}
        # 1. changed constructor keyword
        try:
            BuiltEngine(run_dir=d1, unsandboxed_ok=True, k=2.0)
            refusals["changed_kw"] = False
        except BuiltEngineRefused:
            refusals["changed_kw"] = True
        # 2. lead signature in owner-mode run
        with tempfile.TemporaryDirectory() as d_own:
            _self_test_fixture(d_own, py_src=_mk_py("v1"), sign_by="lead", run_mode="owner")
            try:
                BuiltEngine(run_dir=d_own, unsandboxed_ok=True)
                refusals["lead_in_owner"] = False
            except BuiltEngineRefused:
                refusals["lead_in_owner"] = True
        # 3. spec without build request
        with tempfile.TemporaryDirectory() as d_nb:
            _self_test_fixture(d_nb, py_src=_mk_py("v1"), include_build=False)
            try:
                BuiltEngine(run_dir=d_nb, unsandboxed_ok=True)
                refusals["no_build_req"] = False
            except BuiltEngineRefused:
                refusals["no_build_req"] = True
        # 4. changed results digest
        with tempfile.TemporaryDirectory() as d_res:
            _self_test_fixture(d_res, py_src=_mk_py("v1"))
            open(os.path.join(d_res, "engine_build", "bench", "results.yaml"), "a").write("# edited\n")
            try:
                BuiltEngine(run_dir=d_res, unsandboxed_ok=True)
                refusals["tampered_results"] = False
            except BuiltEngineRefused:
                refusals["tampered_results"] = True
        # 5. changed admission digest
        with tempfile.TemporaryDirectory() as d_adm:
            _self_test_fixture(d_adm, py_src=_mk_py("v1"))
            open(os.path.join(d_adm, "engine_build", "admission.json"), "a").write(" ")
            try:
                BuiltEngine(run_dir=d_adm, unsandboxed_ok=True)
                refusals["tampered_admission"] = False
            except BuiltEngineRefused:
                refusals["tampered_admission"] = True
        # 6. direct BuiltEngine ref in schema without build form
        refusals["direct_schema"] = bool(schema.engine_ref_problems({"engine": {"ref": "toolkit.engines.built:BuiltEngine"}}))
        ok(all(refusals.values()),
           f"BE22 BuiltEngine refuses missing build request, tampered admission/results, lead sig in owner run, "
           f"changed constructor kw, and direct schema ref ({refusals})")

        # BE19 via adapter: tampered snapshot file refused on construction
        sdir = os.path.join(d1, "engine_build", "snapshots", info1["snapshot_sha256"])
        os.chmod(sdir, 0o755)
        ep = os.path.join(sdir, "engine.py")
        os.chmod(ep, 0o644)
        open(ep, "ab").write(b"\n# edited\n")
        try:
            BuiltEngine(run_dir=d1, unsandboxed_ok=True)
            snap_refused = False
        except BuiltEngineRefused:
            snap_refused = True
        ok(snap_refused and bool(BSNAP.verify_engine_snapshot(d1)),
           "BE19e BuiltEngine and verify_engine_snapshot both refuse modified snapshot file")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
