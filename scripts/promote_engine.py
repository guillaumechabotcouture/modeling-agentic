#!/usr/bin/env python3
"""Promote an admitted agent-built engine from a run directory into a candidate team-registry folder
(docs/design/agent-built-engines.md §5.5).

    python3 scripts/promote_engine.py <run_dir> <name> [--out-dir DIR] [--second-run RUN2] [--dry-run]
    python3 scripts/promote_engine.py --self-test

Builds the candidate engine package and benchmark pack from the run's verified admission artifacts
(`engine.py`, `card.yaml`, `ENGINE_NOTES.md`, `attribute_binding.yaml`, `decision_inputs.yaml`,
`benchmark_coverage.yaml`, `benchmarks/pack.yaml`, and `benchmarks/oracles.py` when a reference was
built). Prints a checklist of every remaining requirement for maintainer promotion (two distinct
admitted questions, fresh hidden draws, expert review, owner approval, registry PR) and NEVER edits
any repository registry file (`ENGINES`, `toolkit/engines/__init__.py`, `agents/analysis_backend.py`).
Nothing here names a disease.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib import safe_io as SIO  # noqa: E402
from toolkit.engines.built import adapter as BADAPT  # noqa: E402
from toolkit.engines.built import requested as BREQ  # noqa: E402
from toolkit.engines.built import signoff as BSIG  # noqa: E402
from toolkit.engines.built import snapshot as BSNAP  # noqa: E402

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_single(run_dir: str, rel: str) -> bytes:
    try:
        raw, st = SIO.read_nofollow(run_dir, rel)
    except (FileNotFoundError, SIO.LinkRefused, OSError) as e:
        raise ValueError(f"{rel} unreadable: {e}") from None
    if st.st_nlink > 1:
        raise ValueError(f"{rel} has multiple hard links (st_nlink={st.st_nlink})")
    return raw


def _read_verified_run(run_dir: str) -> dict:
    """Read and verify all promotion-relevant artifacts from a completed/admitted run directory."""
    import yaml

    adm_bytes = _read_single(run_dir, "engine_build/admission.json")
    adm_sha = _sha256_bytes(adm_bytes)
    try:
        adm = json.loads(adm_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"engine_build/admission.json invalid JSON: {e}") from None
    if not isinstance(adm, dict) or adm.get("status") != "admitted":
        raise ValueError(f"built engine in {run_dir} is not admitted (status={adm.get('status') if isinstance(adm, dict) else None!r})")
    snap_sha = adm.get("snapshot_sha256")
    if not isinstance(snap_sha, str) or len(snap_sha) != 64:
        raise ValueError("engine_build/admission.json missing valid snapshot_sha256")
    snaps_dir = os.path.join(run_dir, "engine_build", "snapshots")
    snap_files = BSNAP.read_verified_snapshot(snaps_dir, snap_sha)
    req_bytes = _read_single(run_dir, "engine_build/request.json")
    try:
        req_doc = json.loads(req_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"engine_build/request.json invalid JSON: {e}") from None
    build_decl = req_doc.get("build") if isinstance(req_doc, dict) else None
    if not isinstance(build_decl, Mapping) or not isinstance(build_decl.get("request"), Mapping):
        raise ValueError("engine_build/request.json missing valid build.request")
    if adm.get("engine_id") and build_decl.get("id") != adm.get("engine_id"):
        raise ValueError(f"engine_build/request.json id {build_decl.get('id')!r} != admission engine_id {adm.get('engine_id')!r}")
    if adm.get("request_sha256") != BREQ.request_sha256(build_decl):
        raise ValueError("engine_build/request.json does not match admission.json request_sha256")

    # Verify mandatory bench/* and optional bench/fitted.yaml against admission digests
    digests = adm.get("digests") if isinstance(adm.get("digests"), Mapping) else {}
    bench_bytes: dict[str, bytes] = {}
    for rel in BADAPT.BENCH_FILES:
        want = digests.get(rel)
        raw = _read_single(run_dir, f"engine_build/{rel}")
        if not want or _sha256_bytes(raw) != want:
            raise ValueError(f"engine_build/{rel} digest mismatch against admission.json")
        bench_bytes[rel] = raw
    for rel in BADAPT.OPTIONAL_BENCH_FILES:
        want = digests.get(rel)
        on_disk = os.path.lexists(os.path.join(run_dir, "engine_build", rel))
        if want is None and not on_disk:
            continue
        if not want:
            raise ValueError(f"engine_build/{rel} present without digest in admission.json")
        raw = _read_single(run_dir, f"engine_build/{rel}")
        if _sha256_bytes(raw) != want:
            raise ValueError(f"engine_build/{rel} digest mismatch against admission.json")
        bench_bytes[rel] = raw

    # Optional sign-off record + authoritative sign-off verification via BSIG.status
    signoff_doc = None
    if os.path.lexists(os.path.join(run_dir, "engine_build", "signoff.json")):
        try:
            signoff_doc = json.loads(_read_single(run_dir, "engine_build/signoff.json").decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise ValueError(f"engine_build/signoff.json unreadable: {e}") from None
    signoff_status = BSIG.status(run_dir)

    # Independent A11 oracle: exported ONLY when A11 actually passed AND the recorded reference
    # snapshot verifies byte-for-byte against engine_build/reference/snapshots/<ref_sha>.
    ref_py: bytes | None = None
    a11_status = ((adm.get("checks") or {}).get("A11") or {}).get("status")
    ref_sha = digests.get("reference_snapshot")
    ref_snaps = os.path.join(run_dir, "engine_build", "reference", "snapshots")
    if a11_status == "pass" and isinstance(ref_sha, str) and len(ref_sha) == 64 and os.path.isdir(ref_snaps):
        try:
            ref_files = BSNAP.read_verified_snapshot(ref_snaps, ref_sha)
            ref_py = ref_files.get("engine.py")
        except BSNAP.SnapshotError:
            ref_py = None

    # Read question text if present for two-question check (§5.5)
    question = None
    if os.path.lexists(os.path.join(run_dir, "decision_spec.yaml")):
        try:
            spec_doc = yaml.safe_load(_read_single(run_dir, "decision_spec.yaml").decode("utf-8"))
            if isinstance(spec_doc, dict) and isinstance(spec_doc.get("question"), str):
                question = spec_doc["question"].strip()
        except Exception:  # noqa: BLE001
            question = None
    if not question and os.path.lexists(os.path.join(run_dir, "run_state.json")):
        try:
            rs = json.loads(_read_single(run_dir, "run_state.json").decode("utf-8"))
            if isinstance(rs, dict) and isinstance(rs.get("question"), str):
                question = rs["question"].strip()
        except Exception:  # noqa: BLE001
            question = None

    return {
        "run_dir": os.path.abspath(run_dir),
        "admission": adm,
        "admission_sha256": adm_sha,
        "snapshot_sha256": snap_sha,
        "snapshot_files": snap_files,
        "build_decl": dict(build_decl),
        "bench_bytes": bench_bytes,
        "signoff": signoff_doc,
        "signoff_status": signoff_status,
        "reference_py": ref_py,
        "question": question,
    }


def _strip_future_imports(src: str) -> tuple[str, list[str]]:
    """Extract and remove `from __future__ import ...` statements so the promoted module keeps a
    single valid future-import block at the top of the file."""
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    features: list[str] = ["annotations"]
    drop_spans: list[tuple[int, int]] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            for a in node.names:
                if a.name not in features:
                    features.append(a.name)
            end = getattr(node, "end_lineno", node.lineno) or node.lineno
            drop_spans.append((node.lineno, end))
    if not drop_spans:
        return src, features
    for lo, hi in sorted(drop_spans, reverse=True):
        del lines[lo - 1:hi]
    return "".join(lines), features


def _rewrite_sibling_imports(src: str, sibling_mods: set[str]) -> str:
    """Rewrite bare imports of sibling snapshot `.py` modules to package-relative imports so
    importing `toolkit.engines.<name>.engine` from repo root works without `_SnapshotFinder`."""
    if not sibling_mods:
        return src
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    edits: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top = node.module.split(".")[0]
            if top not in sibling_mods:
                continue
            lo = node.lineno
            hi = getattr(node, "end_lineno", lo) or lo
            first_line = lines[lo - 1]
            indent = first_line[:len(first_line) - len(first_line.lstrip(" \t"))]
            names_s = ", ".join(f"{a.name} as {a.asname}" if a.asname else a.name for a in node.names)
            edits.append((lo, hi, f"{indent}from .{node.module} import {names_s}\n"))
        elif isinstance(node, ast.Import):
            sibs = [a for a in node.names if a.name.split(".")[0] in sibling_mods]
            if not sibs:
                continue
            lo = node.lineno
            hi = getattr(node, "end_lineno", lo) or lo
            first_line = lines[lo - 1]
            indent = first_line[:len(first_line) - len(first_line.lstrip(" \t"))]
            keep = [a for a in node.names if a.name.split(".")[0] not in sibling_mods]
            out_lines: list[str] = []
            if keep:
                out_lines.append(
                    f"{indent}import " + ", ".join(f"{a.name} as {a.asname}" if a.asname else a.name for a in keep) + "\n"
                )
            for a in sibs:
                if "." not in a.name:
                    tail = f" as {a.asname}" if a.asname else ""
                    out_lines.append(f"{indent}from . import {a.name}{tail}\n")
                else:
                    mod_p, leaf = a.name.rsplit(".", 1)
                    tail = f" as {a.asname}" if a.asname else ""
                    out_lines.append(f"{indent}from .{mod_p} import {leaf}{tail}\n")
            edits.append((lo, hi, "".join(out_lines)))
    if not edits:
        return src
    for lo, hi, repl in sorted(edits, key=lambda x: x[0], reverse=True):
        lines[lo - 1:hi] = [repl]
    return "".join(lines)


def _self_test_case_from_request(build_decl: Mapping) -> dict:
    """Derive a valid contract self-test (unit attributes, package components, horizon) from the signed request."""
    req = build_decl.get("request") or {}
    unit_attrs = dict(((req.get("seed_test_setting") or {}).get("unit")) or {})
    if not unit_attrs:
        for aname, aspec in (req.get("unit_attributes") or {}).items():
            if not isinstance(aspec, Mapping):
                continue
            rng = aspec.get("range") if "range" in aspec else aspec.get("test_range")
            if isinstance(rng, (list, tuple)) and len(rng) == 2:
                unit_attrs[str(aname)] = 0.5 * (float(rng[0]) + float(rng[1]))
            elif aspec.get("values"):
                unit_attrs[str(aname)] = aspec["values"][0]
    comps = req.get("components") or {}
    pkg_absent = {str(c): cs["absent"] for c, cs in comps.items() if isinstance(cs, Mapping) and "absent" in cs}
    pkg_active = dict(pkg_absent)
    for c, cs in sorted(comps.items()):
        if isinstance(cs, Mapping):
            alts = [v for v in (cs.get("values") or []) if v != cs.get("absent")]
            if alts:
                pkg_active[str(c)] = alts[0]
                break
    out_wins = list(((req.get("outputs") or {}).get("windows") or {}).keys())
    hor_d = None
    for red in (req.get("reductions") or []):
        if isinstance(red, Mapping) and isinstance(red.get("horizon"), Mapping):
            h = red["horizon"]
            if "start" in h and "stop" in h and "window" in h:
                wins = {str(k): list(v) for k, v in (h.get("windows") or {}).items()}
                for w in out_wins:
                    wins.setdefault(str(w), [float(h["window"][0]), float(h["window"][1])])
                hor_d = {
                    "start": float(h["start"]),
                    "stop": float(h["stop"]),
                    "window": [float(h["window"][0]), float(h["window"][1])],
                    "windows": wins,
                }
                break
    if hor_d is None:
        hor_d = {
            "start": 0.0,
            "stop": 1.0,
            "window": [0.0, 1.0],
            "windows": {str(w): [0.0, 1.0] for w in out_wins},
        }
    return {"unit": unit_attrs, "package": pkg_active, "horizon": hor_d}


def _render_promoted_engine_py(
    name: str,
    snap_files: Mapping[str, bytes],
    build_decl: Mapping,
    *,
    sibling_mods: set[str] | None = None,
) -> bytes:
    """Wrap the verified snapshot `engine.py` into a standalone registry-ready Engine module with
    preserved default config, `resolve_params`, Starsim seed + 4-arg `read_outcomes`, authoritative
    `age_shares`, `fitted_path` support, relative sibling imports, and contract `--self-test` (§5.5)."""
    kind = str(build_decl.get("kind") or "closed_form")
    orig_src = snap_files["engine.py"].decode("utf-8")
    orig_src, future_feats = _strip_future_imports(orig_src)
    orig_src = _rewrite_sibling_imports(orig_src, sibling_mods or set())
    cls_name = "".join(p.capitalize() for p in name.split("_")) + "Engine"
    default_cfg = dict((((build_decl.get("request") or {}).get("configs") or {}).get("default")) or {})
    st_case = _self_test_case_from_request(build_decl)
    future_line = f"from __future__ import {', '.join(future_feats)}"
    wrapper = f'''#!/usr/bin/env python3
"""Promoted engine `{name}` (generated by scripts/promote_engine.py from verified snapshot).

Original kind: {kind}. Review code and notes thoroughly before registering in toolkit/engines/.
"""
{future_line}

import copy as _copy
import math as _math
import os as _os
from collections.abc import Mapping as _Mapping

# --- Verified snapshot source (engine.py) ---
{orig_src.rstrip()}


# --- Registry Engine wrapper ---
_HERE = _os.path.dirname(_os.path.abspath(__file__))
_DEFAULT_CONFIG = {default_cfg!r}
_DEFAULT_FITTED = _os.path.join(_HERE, "benchmarks", "fitted.yaml")
_DEFAULT_RESULTS = _os.path.join(_HERE, "benchmarks", "results.yaml")
_SELF_TEST_CASE = {st_case!r}
_AGE_POP_PRIORITY = (
    "age_population", "age_populations", "age_pop", "population_by_age", "pop_by_age",
    "age_shares", "age_weights", "age_distribution",
)
_AGE_POP_INCLUDE = ("pop", "share", "weight", "dist")
_AGE_POP_EXCLUDE = ("contact", "rate", "mort", "hazard", "eff")


def _normalize_age_vec(age_groups: list[str], vec) -> dict[str, float] | None:
    if not (isinstance(vec, (list, tuple)) and len(vec) == len(age_groups)):
        return None
    try:
        fv = [float(x) for x in vec]
    except (TypeError, ValueError):
        return None
    tot = sum(fv)
    if tot > 0 and all(_math.isfinite(x) and x >= 0 for x in fv):
        return {{str(g): x / tot for g, x in zip(age_groups, fv)}}
    return None


def _compute_age_shares(age_groups: list[str], unit_attrs: _Mapping) -> dict[str, float]:
    attrs = unit_attrs or {{}}
    lower_map = {{str(k).lower(): attrs[k] for k in sorted(attrs, key=str)}}
    for pname in _AGE_POP_PRIORITY:
        if pname in lower_map:
            norm = _normalize_age_vec(age_groups, lower_map[pname])
            if norm is not None:
                return norm
    for k in sorted(lower_map):
        if ("age" in k and any(tok in k for tok in _AGE_POP_INCLUDE)
                and not any(tok in k for tok in _AGE_POP_EXCLUDE)):
            norm = _normalize_age_vec(age_groups, lower_map[k])
            if norm is not None:
                return norm
    n = max(len(age_groups), 1)
    return {{str(g): 1.0 / n for g in age_groups}}


class {cls_name}:
    """Registry Engine adapter wrapping promoted `Model`."""

    def __init__(self, *, fitted_path: str | None = None, results_path: str | None = None, **config):
        merged = {{**_DEFAULT_CONFIG, **config}}
        self._config = dict(merged)
        self._model = Model(**merged)
        self.fitted_path = fitted_path if fitted_path is not None else (
            _DEFAULT_FITTED if _os.path.isfile(_DEFAULT_FITTED) else None
        )
        self.results_path = results_path if results_path is not None else (
            _DEFAULT_RESULTS if _os.path.isfile(_DEFAULT_RESULTS) else None
        )

    def describe(self):
        return self._model.describe()

    def parameters(self):
        return self._model.parameters()

    def run(self, params, unit, package, horizon, seed):
        from toolkit.engines import contract as _C
        resolved = _C.resolve_params(self, params)
        card = self.describe()
        raw_attrs = _copy.deepcopy(unit.attributes or {{}})
        age_shares = _compute_age_shares(list(card.age_groups), raw_attrs)
        safe_unit = _C.UnitSpec(unit.id, raw_attrs)
        if hasattr(self._model, "evaluate"):
            out = self._model.evaluate(resolved, safe_unit, package, horizon)
        else:
            sim = self._model.build_sim(resolved, safe_unit, package, horizon)
            sim.pars.rand_seed = int(seed)
            sim.run()
            out = self._model.read_outcomes(sim, safe_unit, package, horizon)
        meta = dict(out.meta or {{}})
        meta["age_shares"] = age_shares
        return _C.Outcomes(
            per_capita=out.per_capita,
            windows=out.windows,
            cost_drivers=out.cost_drivers,
            meta=meta,
        )


def _self_test() -> int:
    import sys as _sys
    root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_HERE)))
    if _os.path.isdir(_os.path.join(root, "toolkit")) and root not in _sys.path:
        _sys.path.insert(0, root)
    from toolkit.engines import contract as _C
    eng = {cls_name}()
    unit = _C.UnitSpec("self_test", dict(_SELF_TEST_CASE["unit"]))
    pkg = _C.Package("self_test", dict(_SELF_TEST_CASE["package"]))
    h = _SELF_TEST_CASE["horizon"]
    hor = _C.Horizon(float(h["start"]), float(h["stop"]), tuple(h["window"]), dict(h.get("windows") or {{}}))
    errs = _C.check_engine(eng, unit, pkg, hor, seed=1)
    if errs:
        for e in errs:
            print("FAIL", e)
        return 1
    print("OK: promoted engine passed contract.check_engine.")
    return 0


if __name__ == "__main__":
    import sys as _sys
    if _sys.argv[1:2] == ["--self-test"]:
        _sys.exit(_self_test())
'''
    compile(wrapper, f"{name}/engine.py", "exec")
    return wrapper.encode("utf-8")


def _rewrite_pack_yaml(pack_bytes: bytes, name: str, cls_name: str) -> bytes:
    """Retarget `benchmarks/pack.yaml`'s `engine` field from `BuiltEngine` to the promoted class."""
    import yaml
    doc = yaml.safe_load(pack_bytes.decode("utf-8"))
    if isinstance(doc, dict):
        doc = copy.deepcopy(doc)
        doc["engine"] = f"toolkit.engines.{name}.engine:{cls_name}"
        return yaml.safe_dump(doc, sort_keys=False).encode("utf-8")
    return pack_bytes


def promotion_checklist(run1: dict, run2: dict | None = None) -> list[dict]:
    """Evaluate §5.5 promotion requirements and return `[{id, ok, detail}]`."""
    items: list[dict] = []
    so1 = run1.get("signoff") or {}
    so1_sha = so1.get("sha256") if isinstance(so1.get("sha256"), Mapping) else {}
    owner_ok = (
        run1.get("signoff_status") == "signed"
        and so1.get("by") == "owner"
        and so1.get("snapshot_sha256") == run1.get("snapshot_sha256")
        and so1_sha.get("admission.json") == run1.get("admission_sha256")
    )
    items.append({
        "id": "owner_signed",
        "ok": owner_ok,
        "detail": (
            "owner-signed and verified in source run"
            if owner_ok
            else f"source run sign-off is {so1.get('by')!r} (status={run1.get('signoff_status')!r}); valid owner sign-off matching snapshot and admission required"
        ),
    })
    if run2 is None:
        items.append({
            "id": "second_question",
            "ok": False,
            "detail": "missing second admitted question (--second-run <run_dir2>); §5.5 requires admission on two distinct questions",
        })
    else:
        q1, q2 = run1.get("question") or "", run2.get("question") or ""
        same_id = run1["build_decl"].get("id") == run2["build_decl"].get("id")
        same_snap = run1.get("snapshot_sha256") == run2.get("snapshot_sha256")
        distinct_q = bool(q1 and q2 and q1 != q2)
        items.append({
            "id": "second_question",
            "ok": bool(same_id and same_snap and distinct_q),
            "detail": (
                f"same snapshot ({run1['snapshot_sha256'][:12]}) admitted on two distinct questions for engine {run1['build_decl'].get('id')!r}"
                if (same_id and same_snap and distinct_q)
                else "second run must admit the exact same engine snapshot and id on a distinct question"
            ),
        })
    items.append({
        "id": "reference_oracle",
        "ok": run1.get("reference_py") is not None,
        "detail": (
            "verified passing A11 reference exported to benchmarks/oracles.py"
            if run1.get("reference_py") is not None
            else "no verified passing A11 reference snapshot found; add benchmarks/oracles.py"
        ),
    })
    items.append({
        "id": "fresh_hidden_draws",
        "ok": False,
        "detail": "generate fresh hidden benchmark draws at promotion time and restamp benchmarks/results.yaml",
    })
    items.append({
        "id": "expert_review_and_pr",
        "ok": False,
        "detail": "complete expert review of code + ENGINE_NOTES.md, obtain owner approval, run codex_review.py + CI, and register in ENGINES via maintainer PR",
    })
    return items


def promote(
    run_dir: str,
    name: str,
    *,
    out_dir: str | None = None,
    second_run: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Build a candidate promotion folder for `<name>` from `<run_dir>` without modifying any registry file."""
    import yaml

    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError(f"invalid engine name {name!r}: must match {_NAME_RE.pattern}")
    run1 = _read_verified_run(run_dir)
    run2 = _read_verified_run(second_run) if second_run else None
    checklist = promotion_checklist(run1, run2)
    missing = [c for c in checklist if not c["ok"]]

    cls_name = "".join(p.capitalize() for p in name.split("_")) + "Engine"
    req_eng = BREQ.RequestedEngine(run1["build_decl"])
    snap_files = run1["snapshot_files"]
    sibling_mods = {fn[:-3] for fn in snap_files if fn.endswith(".py") and fn != "engine.py"}

    out_files: dict[str, bytes] = {
        "__init__.py": (
            f'"""Promoted candidate engine `{name}`."""\n'
            f"from .engine import {cls_name}\n\n"
            f'__all__ = ["{cls_name}"]\n'
        ).encode("utf-8"),
        "engine.py": _render_promoted_engine_py(name, snap_files, run1["build_decl"], sibling_mods=sibling_mods),
        "card.yaml": snap_files["card.yaml"],
        "ENGINE_NOTES.md": snap_files["ENGINE_NOTES.md"],
        "attribute_binding.yaml": yaml.safe_dump(
            {"attribute_binding": req_eng.attribute_binding()}, sort_keys=False
        ).encode("utf-8"),
        "decision_inputs.yaml": yaml.safe_dump(
            {"decision_inputs": req_eng.decision_inputs_declaration()}, sort_keys=False
        ).encode("utf-8"),
        "benchmark_coverage.yaml": run1["bench_bytes"]["bench/benchmark_coverage.yaml"],
        "benchmarks/pack.yaml": _rewrite_pack_yaml(run1["bench_bytes"]["bench/pack.yaml"], name, cls_name),
        "benchmarks/results_from_run.yaml": run1["bench_bytes"]["bench/results.yaml"],
    }
    if "bench/fitted.yaml" in run1["bench_bytes"]:
        out_files["benchmarks/fitted.yaml"] = run1["bench_bytes"]["bench/fitted.yaml"]
    for fname, raw in sorted(snap_files.items()):
        if fname.endswith(".py") and fname != "engine.py":
            rewritten = _rewrite_sibling_imports(raw.decode("utf-8"), sibling_mods)
            compile(rewritten, f"{name}/{fname}", "exec")
            out_files[fname] = rewritten.encode("utf-8")
    if run1.get("reference_py") is not None:
        out_files["benchmarks/oracles.py"] = run1["reference_py"]

    written_dir = None
    if not dry_run:
        target_root = os.path.abspath(out_dir or os.path.join(run1["run_dir"], "engine_build", "promotion_candidate"))
        engines_real = os.path.realpath(os.path.join(ROOT, "toolkit", "engines"))
        target_real = os.path.realpath(target_root)
        if target_real == engines_real or target_real.startswith(engines_real + os.sep):
            raise ValueError(
                f"refusing to write directly into repository engine registry {engines_real}; "
                "export to a candidate folder and open a maintainer PR (§5.5)"
            )
        dest = os.path.join(target_root, name)
        if os.path.lexists(dest):
            raise ValueError(f"target directory already exists: {dest}; choose a fresh --out-dir or remove it first")
        os.makedirs(target_root, exist_ok=True)
        try:
            os.makedirs(dest, exist_ok=False)
        except FileExistsError:
            raise ValueError(f"target directory already exists: {dest}; choose a fresh --out-dir or remove it first") from None
        for rel, data in sorted(out_files.items()):
            SIO.write_nofollow(dest, rel, data, atomic=True)
        written_dir = dest

    return {
        "name": name,
        "source_run": run1["run_dir"],
        "snapshot_sha256": run1["snapshot_sha256"],
        "reuse_key": run1["admission"].get("reuse_key"),
        "dry_run": bool(dry_run),
        "written_dir": written_dir,
        "files": sorted(out_files.keys()),
        "checklist": checklist,
        "missing": [c["id"] for c in missing],
        "registry_modified": False,
    }


def _self_test() -> int:
    import importlib
    import tempfile
    import yaml
    from toolkit.engines import contract as _C
    from toolkit.engines import decision_inputs as DI
    from toolkit.spec import schema as _SCH

    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    src_py = (
        '"""Fixture model with future import, config, default param, age_pop, and sibling import."""\n'
        "from __future__ import annotations\n"
        "from rates import scale_rate\n"
        "from toolkit.engines.contract import EngineCard, Outcomes, ParamSpec\n"
        "class Model:\n"
        "    def __init__(self, dt_step=1.0):\n"
        "        self.dt_step = float(dt_step)\n"
        "    def describe(self):\n"
        "        return EngineCard(name='built:demo_mod', version='0.1', mechanisms=['m'],\n"
        "                          components={'comp_a': [0, 1]},\n"
        "                          unit_attributes={'N': 'n', 'rate_in': 'r', 'age_pop': 'pop'},\n"
        "                          outputs={'per_capita': ['cases'], 'windows': ['prev'], 'cost_drivers': ['units_delivered']},\n"
        "                          age_groups=['young', 'old'], valid_ranges={'rate_in': [0.01, 0.5]})\n"
        "    def parameters(self):\n"
        "        return [ParamSpec('eff_a', 0.5, 'fitted', lo=0.2, hi=0.8, source='dossier:E1')]\n"
        "    def evaluate(self, params, unit, package, horizon):\n"
        "        r = scale_rate(float(unit.attributes['rate_in']), self.dt_step)\n"
        "        eff = 1.0 - float(params['eff_a']) * int(package.components.get('comp_a', 0))\n"
        "        return Outcomes(per_capita={'cases': {'young': r * eff, 'old': 9.0 * r * eff}},\n"
        "                        windows={'w0': {'prev': r * eff}},\n"
        "                        cost_drivers={'units_delivered': float(package.components.get('comp_a', 0))})\n"
    ).encode("utf-8")
    rates_py = b"def scale_rate(r, dt_step):\n    return r * dt_step\n"

    req_decl = {
        "id": "demo_mod",
        "kind": "closed_form",
        "why": "synthetic test",
        "mechanisms": ["m"],
        "request": {
            "stochastic": False,
            "configs": {"default": {"dt_step": 0.25}},
            "components": {"comp_a": {"values": [0, 1], "absent": 0}},
            "unit_attributes": {
                "N": {"type": "continuous", "range": [100.0, 10000.0]},
                "rate_in": {"type": "continuous", "range": [0.01, 0.5]},
                "age_pop": {"type": "vector", "length": 2, "test_range": [10.0, 100.0]},
            },
            "outputs": {"per_capita": ["cases"], "windows": {"w0": ["prev"]}, "cost_drivers": ["units_delivered"]},
            "age_groups": ["young", "old"],
            "parameters": [{"name": "eff_a", "value": 0.5, "kind": "fitted", "lo": 0.2, "hi": 0.8,
                            "units": "share", "source": "dossier:E1"}],
            "attribute_binding": {"N": "required", "rate_in": "required", "age_pop": "required"},
            "decision_inputs": {"comp_a": [{"kind": "parameter", "name": "eff_a", "why": "effect"}]},
            "expected_effects": {"comp_a": {"cases": "-", "units_delivered": "+"}, "eff_a": {"cases": "-"},
                                 "rate_in": {"cases": "+"}},
            "inert": {"N": {"reason": "invariant"}, "age_pop": {"reason": "weights only"}},
            "seed_test_setting": {"unit": {"N": 1000.0, "rate_in": 0.1, "age_pop": [90.0, 10.0]}},
            "reductions": [{"id": "R1", "oracle": "constant_hazard_survival", "oracle_args": {"hazard": 0.1, "dt": 1.0},
                            "set": {"comp_a": 0}, "unit": {"N": 1000.0, "rate_in": 0.1, "age_pop": [90.0, 10.0]},
                            "horizon": {"start": 0.0, "stop": 1.0, "window": [0.0, 1.0], "windows": {"w0": [0.0, 1.0]}},
                            "compare": {"windows.w0.prev": {"abs": 0.5}}}],
            "trials": [],
            "reference_tolerance": {"abs": 0.02, "rel": 0.04},
        },
    }

    def _build_run(rd: str, *, sign_by: str, run_mode: str, question: str,
                   extra_src: bytes = b"", a11_status: str = "pass", with_fitted: bool = True) -> dict:
        os.makedirs(rd, exist_ok=True)
        work = os.path.join(rd, "models", "ext", "engine_demo_mod")
        os.makedirs(work, exist_ok=True)
        open(os.path.join(work, "engine.py"), "wb").write(src_py + extra_src)
        open(os.path.join(work, "rates.py"), "wb").write(rates_py)
        open(os.path.join(work, "card.yaml"), "wb").write(b"name: built:demo_mod\nversion: '0.1'\nmechanisms: [m]\n")
        open(os.path.join(work, "ENGINE_NOTES.md"), "wb").write(b"# notes\n")
        snaps = os.path.join(rd, "engine_build", "snapshots")
        man = BSNAP.create_snapshot(work, snaps, run_static=False)
        snap_sha = man["snapshot_sha256"]
        os.chmod(os.path.join(snaps, snap_sha), 0o755)
        ref_sha = None
        if a11_status == "pass":
            ref_snaps = os.path.join(rd, "engine_build", "reference", "snapshots")
            rman = BSNAP.create_reference_snapshot(src_py.replace(b"from rates import scale_rate\n", b"").replace(b"scale_rate(float(unit.attributes['rate_in']), self.dt_step)", b"float(unit.attributes['rate_in']) * self.dt_step"), ref_snaps)
            ref_sha = rman["snapshot_sha256"]
            os.chmod(os.path.join(ref_snaps, ref_sha), 0o755)
        bench_files = {
            "bench/pack.yaml": b"engine: toolkit.engines.built:BuiltEngine\nbenchmarks: []\n",
            "bench/results.yaml": b"benchmarks: []\n",
            "bench/benchmark_coverage.yaml": b"engine: built:demo_mod\nbenchmarks: {}\n",
        }
        if with_fitted:
            bench_files["bench/fitted.yaml"] = b"parameters:\n  eff_a: {value: 0.5, lo: 0.2, hi: 0.8}\n"
        digests = {}
        for rel, blob in bench_files.items():
            SIO.write_nofollow(rd, f"engine_build/{rel}", blob, atomic=True)
            digests[rel] = _sha256_bytes(blob)
        if ref_sha is not None:
            digests["reference_snapshot"] = ref_sha
        SIO.write_nofollow(rd, "engine_build/request.json",
                           json.dumps({"build": req_decl}, indent=1).encode("utf-8"), atomic=True)
        adm = {
            "status": "admitted",
            "engine_id": "demo_mod",
            "kind": "closed_form",
            "request_sha256": BREQ.request_sha256(req_decl),
            "snapshot_sha256": snap_sha,
            "config_sha256": BADAPT.effective_config_sha256({"dt_step": 0.25}),
            "checks": {"A1": {"status": "pass", "passed": 1, "total": 1}, "A11": {"status": a11_status, "passed": 1, "total": 1}},
            "digests": digests,
        }
        adm_bytes = json.dumps(adm, indent=1, sort_keys=True).encode("utf-8")
        SIO.write_nofollow(rd, "engine_build/admission.json", adm_bytes, atomic=True)
        sheet_bytes = b"# Admission sheet\n"
        SIO.write_nofollow(rd, "engine_build/admission_sheet.md", sheet_bytes, atomic=True)
        SIO.write_nofollow(rd, "run_state.json", json.dumps({"signoff": run_mode, "question": question}).encode("utf-8"), atomic=True)
        if sign_by == "lead":
            SIO.write_nofollow(rd, BSIG.REVIEW_STATUS_REL,
                               json.dumps({"snapshot_sha256": snap_sha, "review_unavailable": False, "high_count": 0}).encode("utf-8"),
                               atomic=True)
        BSIG.sign(rd, sign_by, note="test")
        return {"snapshot_sha256": snap_sha, "admission_sha256": _sha256_bytes(adm_bytes)}

    with tempfile.TemporaryDirectory() as td:
        r1 = os.path.join(td, "run1")
        r2 = os.path.join(td, "run2")
        r_diff = os.path.join(td, "run_diff_snap")
        r_skip = os.path.join(td, "run_skip_a11")
        out_root = os.path.join(td, "promo_out")
        _build_run(r1, sign_by="lead", run_mode="lead", question="Question alpha?")
        _build_run(r2, sign_by="owner", run_mode="owner", question="Question beta?")
        _build_run(r_diff, sign_by="owner", run_mode="owner", question="Question gamma?", extra_src=b"\n# v2\n")
        _build_run(r_skip, sign_by="owner", run_mode="owner", question="Question delta?", a11_status="skipped")
        # Plant stray working reference.py on skipped-A11 run (must NOT be exported!)
        SIO.write_nofollow(r_skip, "engine_build/reference/reference.py", b"class Model:\n    pass\n", atomic=True)

        # PROM1: dry promotion of lead-signed run lists missing owner sign-off & 2nd question
        res_dry = promote(r1, "demo_promoted", dry_run=True)
        ok(res_dry["dry_run"] is True and res_dry["written_dir"] is None and res_dry["registry_modified"] is False
           and "owner_signed" in res_dry["missing"] and "second_question" in res_dry["missing"]
           and "benchmarks/oracles.py" in res_dry["files"] and "benchmarks/fitted.yaml" in res_dry["files"],
           f"PROM1 dry promotion checks missing items and includes verified oracles/fitted ({res_dry['missing']})")

        # PROM2: full promotion writes package, compiles with __future__, preserves default config/resolve_params/age_shares/
        # sibling relative imports/fitted_path, retargets pack.yaml, wraps binding/decision_inputs, and passes _self_test
        res_full = promote(r2, "demo_promoted", out_dir=out_root, second_run=r1)
        wdir = res_full["written_dir"]
        ab_map, ab_errs = _SCH._read_binding(os.path.join(wdir, "attribute_binding.yaml"))
        di_map = DI.load(wdir)
        pack_doc = yaml.safe_load(open(os.path.join(wdir, "benchmarks", "pack.yaml")))
        # Import promoted package as a package to test sibling relative imports, default config, resolve_params, age_shares, self_test
        sys.path.insert(0, out_root)
        try:
            pkg_mod = importlib.import_module("demo_promoted.engine")
            eng = pkg_mod.DemoPromotedEngine()
            out_run = eng.run({}, _C.UnitSpec("u", {"N": 1000.0, "rate_in": 0.1, "age_pop": [90.0, 10.0]}),
                              _C.Package("p", {"comp_a": 1}), _C.Horizon(0, 1, (0, 1), {"w0": [0, 1]}), 1)
            st_rc = pkg_mod._self_test()
        finally:
            sys.path.remove(out_root)
            for mk in [k for k in sys.modules if k == "demo_promoted" or k.startswith("demo_promoted.")]:
                sys.modules.pop(mk, None)
        agg = sum(out_run.per_capita["cases"][g] * out_run.meta["age_shares"][g] for g in ("young", "old"))
        # dt_step=0.25, rate=0.1 -> r=0.025, eff=0.5 -> young=0.0125, old=0.1125; age_shares={young:0.9, old:0.1} -> agg=0.0225
        ok(wdir is not None and not ab_errs and "N" in ab_map and isinstance(di_map, dict) and "comp_a" in di_map
           and pack_doc["engine"] == "toolkit.engines.demo_promoted.engine:DemoPromotedEngine"
           and eng.fitted_path == os.path.join(wdir, "benchmarks", "fitted.yaml")
           and abs(out_run.meta["age_shares"]["young"] - 0.9) < 1e-9
           and abs(agg - 0.0225) < 1e-9 and st_rc == 0
           and "owner_signed" not in res_full["missing"] and "second_question" not in res_full["missing"],
           f"PROM2 promoted package imports cleanly, preserves config/resolve_params/age_shares/fitted_path, wraps YAML, retargets pack, passes _self_test (agg={agg}, rc={st_rc})")

        # PROM3: refusing existing target dir & refusing toolkit/engines out-dir
        refused_existing = refused_registry = False
        try:
            promote(r2, "demo_promoted", out_dir=out_root)
        except ValueError:
            refused_existing = True
        try:
            promote(r2, "demo_promoted", out_dir=os.path.join(ROOT, "toolkit", "engines"))
        except ValueError:
            refused_registry = True
        ok(refused_existing and refused_registry, "PROM3 refuses overwriting existing export dir and refuses toolkit/engines target")

        # PROM4: mismatched second snapshot fails second_question; skipped A11 with stray reference.py does NOT pass reference_oracle
        res_diff = promote(r2, "demo_promoted_b", dry_run=True, second_run=r_diff)
        res_skip = promote(r_skip, "demo_promoted_c", dry_run=True)
        ok("second_question" in res_diff["missing"]
           and "reference_oracle" in res_skip["missing"]
           and "benchmarks/oracles.py" not in res_skip["files"],
           "PROM4 mismatched second snapshot fails second_question; skipped A11 refuses unverified reference.py")

        # PROM5: stale owner sign-off from another snapshot fails owner_signed; post-admission request edit refused; tampered bench refused
        stale_so = open(os.path.join(r_diff, "engine_build", "signoff.json"), "rb").read()
        SIO.write_nofollow(r2, "engine_build/signoff.json", stale_so, atomic=True)
        res_stale = promote(r2, "demo_promoted_d", dry_run=True)
        req_tampered = copy.deepcopy(req_decl)
        req_tampered["request"]["attribute_binding"]["N"] = {"default": 500.0, "meaning": "edited"}
        SIO.write_nofollow(r1, "engine_build/request.json",
                           json.dumps({"build": req_tampered}, indent=1).encode("utf-8"), atomic=True)
        req_edit_refused = bad_name_refused = False
        try:
            promote(r1, "demo_promoted_e", dry_run=True)
        except ValueError:
            req_edit_refused = True
        try:
            promote(r2, "../escape", dry_run=True)
        except ValueError:
            bad_name_refused = True
        ok("owner_signed" in res_stale["missing"] and req_edit_refused and bad_name_refused,
           "PROM5 stale owner sign-off fails owner_signed; edited request.json and invalid engine name refused")

        # PROM6: generated starsim_module wrapper sets sim.pars.rand_seed and calls 4-arg read_outcomes(sim, unit, package, horizon)
        ss_src = (
            "from types import SimpleNamespace\n"
            "from toolkit.engines.contract import EngineCard, Outcomes, ParamSpec\n"
            "class _FakeSim:\n"
            "    def __init__(self):\n"
            "        self.pars = SimpleNamespace(rand_seed=0)\n"
            "        self.ran = False\n"
            "    def run(self):\n"
            "        self.ran = True\n"
            "class Model:\n"
            "    def describe(self):\n"
            "        return EngineCard(name='built:ss_demo', version='0.1', mechanisms=['m'],\n"
            "                          components={'comp_a': [0, 1]}, unit_attributes={'N': 'n'},\n"
            "                          outputs={'per_capita': ['cases'], 'windows': ['prev'], 'cost_drivers': ['d']},\n"
            "                          age_groups=['all'], valid_ranges={'N': [10, 1000]})\n"
            "    def parameters(self):\n"
            "        return [ParamSpec('p', 2.0, 'fixed', lo=1.0, hi=3.0, source='dossier:E1')]\n"
            "    def build_sim(self, params, unit, package, horizon):\n"
            "        sim = _FakeSim()\n"
            "        sim.seen_p = params['p']\n"
            "        return sim\n"
            "    def read_outcomes(self, sim, unit, package, horizon):\n"
            "        val = float(sim.pars.rand_seed) + float(sim.seen_p)\n"
            "        return Outcomes(per_capita={'cases': {'all': val}}, windows={'w0': {'prev': 0.1}}, cost_drivers={'d': 0.0})\n"
        ).encode("utf-8")
        ss_code = _render_promoted_engine_py(
            "ss_demo",
            {"engine.py": ss_src, "card.yaml": b"name: built:ss_demo\n", "ENGINE_NOTES.md": b"# n\n"},
            {"id": "ss_demo", "kind": "starsim_module", "request": dict(req_decl["request"], configs={})},
        )
        ss_root = os.path.join(td, "ss_out")
        os.makedirs(os.path.join(ss_root, "ss_demo"), exist_ok=True)
        SIO.write_nofollow(os.path.join(ss_root, "ss_demo"), "__init__.py", b"", atomic=True)
        SIO.write_nofollow(os.path.join(ss_root, "ss_demo"), "engine.py", ss_code, atomic=True)
        sys.path.insert(0, ss_root)
        try:
            ss_mod = importlib.import_module("ss_demo.engine")
            ss_eng = ss_mod.SsDemoEngine()
            ss_out = ss_eng.run({}, _C.UnitSpec("u", {"N": 100.0}), _C.Package("p", {"comp_a": 0}),
                                _C.Horizon(0, 1, (0, 1), {"w0": [0, 1]}), 42)
        finally:
            sys.path.remove(ss_root)
            for mk in [k for k in sys.modules if k == "ss_demo" or k.startswith("ss_demo.")]:
                sys.modules.pop(mk, None)
        ok(abs(ss_out.per_capita["cases"]["all"] - 44.0) < 1e-9,
           f"PROM6 generated starsim wrapper resolves params, sets sim.pars.rand_seed, and calls 4-arg read_outcomes ({ss_out.per_capita})")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", help="Admitted built-engine run directory")
    ap.add_argument("name", help="Candidate registry engine name (lowercase snake_case)")
    ap.add_argument("--out-dir", default=None, help="Output parent directory (default: <run_dir>/engine_build/promotion_candidate)")
    ap.add_argument("--second-run", default=None, help="Second admitted run directory on a distinct question (§5.5)")
    ap.add_argument("--dry-run", action="store_true", help="Inspect and print missing requirements without writing files")
    a = ap.parse_args(argv)
    try:
        res = promote(a.run_dir, a.name, out_dir=a.out_dir, second_run=a.second_run, dry_run=a.dry_run)
    except (ValueError, BSNAP.SnapshotError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
    print(f"Promotion candidate: {res['name']} (snapshot {res['snapshot_sha256'][:12]})")
    if res["written_dir"]:
        print(f"Written to: {res['written_dir']} ({len(res['files'])} files; no registry files modified)")
    print("Checklist:")
    for item in res["checklist"]:
        mark = "OK  " if item["ok"] else "MISS"
        print(f"  [{mark}] {item['id']}: {item['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
