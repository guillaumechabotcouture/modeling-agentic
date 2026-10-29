#!/usr/bin/env python3
"""Phase 19 α — effort floors.

Quantitative gate: was the underlying work done with enough effort?
Reads .claude/orchestration/effort_floors.yaml, walks each floor's probe
against the run directory, writes effort_floors_report.yaml summarizing
violations.

Probes
------
yaml_field              Read a YAML file, follow a dot-path, compare int field
                        to floor.minimum.
yaml_field_list_length  Same, but the path resolves to a list and we compare
                        len(list).
yaml_field_truthy       Same, but field need only be truthy (>= 1 maps to "exists").
artifact_present        Phase 21 γ: the artifact itself must exist (used when
                        other artifacts prove the work happened but the
                        effort-evidence file was never written).
ast_shortcut_scan       Walk *.py under models/, scan AST for shortcut markers
                        (TODO/FIXME/HACK comments, bare `except: pass`, suspiciously
                        small literals on n_replicates/n_draws/maxiter/n_iter).

A missing trigger artifact (e.g., no uncertainty_report.yaml means UQ
was never run) skips the probe rather than firing; the validator decides
separately whether absence of UQ is itself a violation (Phase 2's
rigor-artifact check). Phase 21 γ exception: when *other* artifacts show
calibration happened, a missing calibration_result.yaml is a violation
(`calibration_result_missing`) rather than a silent skip.

Exit status
-----------
0  no violations (or trigger artifacts absent)
1  one or more violations recorded in effort_floors_report.yaml
2  manifest malformed / run_dir missing
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.text_spans import resolve_dotpath as _resolve_dotpath  # noqa: E402

_MANIFEST_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..",
                 ".claude", "orchestration", "effort_floors.yaml")
)

_VALID_SEVERITIES = frozenset({"HIGH", "MEDIUM"})
_VALID_PROBES = frozenset({
    "yaml_field", "yaml_field_list_length",
    "yaml_field_truthy", "ast_shortcut_scan", "artifact_present",
})
_VALID_TRIGGERS = frozenset({
    "always", "uq_present", "cloud_uq_present",
    "sensitivity_present", "calibration_present", "calibration_evidence",
})


@dataclass(frozen=True)
class Floor:
    id: str
    probe: str
    artifact: str
    field: str
    minimum: int
    severity: str
    scope_declarable: bool
    triggered_by: str
    rationale: str
    subtract_field: str = ""


def load_manifest(path: str | None = None) -> tuple[dict, list[Floor]]:
    """Load and validate the manifest. Returns (defaults, floors)."""
    p = path or _MANIFEST_PATH
    with open(p) as f:
        doc = yaml.safe_load(f) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"{p}: top-level must be a mapping")
    defaults = doc.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ValueError(f"{p}: `defaults:` must be a mapping")
    raw_floors = doc.get("floors")
    if not isinstance(raw_floors, list) or not raw_floors:
        raise ValueError(f"{p}: `floors:` must be a non-empty list")
    floors: list[Floor] = []
    seen_ids: set[str] = set()
    for i, entry in enumerate(raw_floors):
        if not isinstance(entry, dict):
            raise ValueError(f"{p}: floors[{i}] must be a mapping")
        for required in ("id", "probe", "artifact", "field", "minimum",
                         "severity", "scope_declarable", "triggered_by",
                         "rationale"):
            if required not in entry:
                raise ValueError(
                    f"{p}: floors[{i}]: missing required field {required!r}")
        if entry["id"] in seen_ids:
            raise ValueError(f"{p}: duplicate floor id {entry['id']!r}")
        seen_ids.add(entry["id"])
        if entry["probe"] not in _VALID_PROBES:
            raise ValueError(
                f"{p}: floors[{i}]: probe must be one of "
                f"{sorted(_VALID_PROBES)}, got {entry['probe']!r}")
        if entry["severity"] not in _VALID_SEVERITIES:
            raise ValueError(
                f"{p}: floors[{i}]: severity must be one of "
                f"{sorted(_VALID_SEVERITIES)}, got {entry['severity']!r}")
        if entry["triggered_by"] not in _VALID_TRIGGERS:
            raise ValueError(
                f"{p}: floors[{i}]: triggered_by must be one of "
                f"{sorted(_VALID_TRIGGERS)}, got {entry['triggered_by']!r}")
        floors.append(Floor(
            id=str(entry["id"]),
            probe=str(entry["probe"]),
            artifact=str(entry["artifact"]),
            field=str(entry["field"]),
            minimum=int(entry["minimum"]),
            severity=str(entry["severity"]),
            scope_declarable=bool(entry["scope_declarable"]),
            triggered_by=str(entry["triggered_by"]),
            rationale=str(entry["rationale"]).strip(),
            subtract_field=str(entry.get("subtract_field") or ""),
        ))
    return defaults, floors


# --------------------------------------------------------------------------- #
# Triggers
# --------------------------------------------------------------------------- #


def _trigger_fires(trigger: str, run_dir: str) -> bool:
    if trigger == "always":
        return True
    if trigger == "uq_present":
        return os.path.exists(os.path.join(run_dir, "uncertainty_report.yaml"))
    if trigger == "cloud_uq_present":
        path = os.path.join(run_dir, "uncertainty_report.yaml")
        if not os.path.exists(path):
            return False
        try:
            with open(path) as f:
                doc = yaml.safe_load(f) or {}
        except (yaml.YAMLError, OSError):
            return False
        if not isinstance(doc, dict):
            return False
        # Phase 21 γ: propagate_uncertainty records `backend: cloud`;
        # older reports annotate with a `cloud:` block or `n_nodes`.
        backend = str(doc.get("backend") or "").strip().lower()
        return bool(backend == "cloud" or doc.get("cloud")
                    or doc.get("n_nodes"))
    if trigger == "sensitivity_present":
        return os.path.exists(
            os.path.join(run_dir, "models", "sensitivity_analysis.yaml"))
    if trigger == "calibration_present":
        return os.path.exists(
            os.path.join(run_dir, "models", "calibration_result.yaml"))
    if trigger == "calibration_evidence":
        return _calibration_evidence(run_dir) is not None
    return False


def _calibration_evidence(run_dir: str) -> str | None:
    """Phase 21 γ: return a short description of an artifact proving
    parameters were fitted, or None. Generic — any fitted-parameter
    model leaves one of these behind."""
    for rel in ("models/identifiability.yaml", "identifiability.yaml"):
        p = os.path.join(run_dir, rel)
        if os.path.exists(p):
            return rel
    for rel in ("models/model_comparison.yaml", "model_comparison.yaml"):
        p = os.path.join(run_dir, rel)
        if not os.path.exists(p):
            continue
        try:
            with open(p) as f:
                doc = yaml.safe_load(f) or {}
        except (yaml.YAMLError, OSError):
            continue
        models = doc.get("models") if isinstance(doc, dict) else None
        if isinstance(models, list):
            for m in models:
                if not isinstance(m, dict):
                    continue
                try:
                    if int(m.get("k_params") or 0) > 0:
                        return f"{rel} (k_params > 0)"
                except (TypeError, ValueError):
                    continue
    models_dir = os.path.join(run_dir, "models")
    if os.path.isdir(models_dir):
        for fname in sorted(os.listdir(models_dir)):
            low = fname.lower()
            # Phase 43 α: the calibration spec declares intent, not a fit.
            if "calibrat" in low and fname not in ("calibration_result.yaml", "calibration_spec.yaml") \
                    and not low.endswith(".py"):
                return f"models/{fname}"
    return None


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #


def _resolve_first(doc: Any, field_spec: str) -> tuple[Any, str]:
    """Phase 21 γ: `a|b` field specs resolve to the first present path.
    Returns (value, path_used)."""
    for alt in field_spec.split("|"):
        alt = alt.strip()
        val = _resolve_dotpath(doc, alt)
        if val is not None:
            return val, alt
    return None, field_spec


def _probe_yaml_field(run_dir: str, floor: Floor) -> tuple[bool, int | None, str]:
    """Return (passes_floor, observed_value_or_None, diagnostic_string)."""
    path = os.path.join(run_dir, floor.artifact)
    if not os.path.exists(path):
        return True, None, f"artifact {floor.artifact} absent (trigger expected to skip)"
    try:
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as e:
        return False, None, f"could not parse {floor.artifact}: {e}"
    val, used = _resolve_first(doc, floor.field)
    if val is None:
        return False, None, (f"field {floor.field!r} absent in "
                             f"{floor.artifact}; floor expects ≥ {floor.minimum}")
    if isinstance(val, bool):
        return False, None, (f"field {used!r} in {floor.artifact} "
                             f"is a boolean, not a count (got {val!r})")
    try:
        n = int(val)
    except (TypeError, ValueError):
        return False, None, (f"field {used!r} in {floor.artifact} "
                             f"is not an integer (got {val!r})")
    # Phase 21 γ: count successes, not requests. With `a|b` specs the
    # first alternative is the already-net count; subtract only when a
    # fallback (or a single-path field) was used.
    diag = ""
    alts = [x.strip() for x in floor.field.split("|")]
    if floor.subtract_field and (len(alts) == 1 or used != alts[0]):
        sub, sub_used = _resolve_first(doc, floor.subtract_field)
        if sub is not None and not isinstance(sub, bool):
            try:
                k = int(sub)
                if k > 0:
                    diag = (f"{used}={n} minus {sub_used}={k} "
                            f"→ {n - k} effective")
                    n -= k
            except (TypeError, ValueError):
                pass
    return n >= floor.minimum, n, diag


def _probe_yaml_field_list_length(run_dir: str,
                                    floor: Floor) -> tuple[bool, int | None, str]:
    path = os.path.join(run_dir, floor.artifact)
    if not os.path.exists(path):
        return True, None, f"artifact {floor.artifact} absent"
    try:
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as e:
        return False, None, f"could not parse {floor.artifact}: {e}"
    val = _resolve_dotpath(doc, floor.field)
    if val is None:
        return False, None, (f"path {floor.field!r} absent in "
                             f"{floor.artifact}")
    if not isinstance(val, list):
        return False, None, (f"path {floor.field!r} in {floor.artifact} "
                             f"is not a list (got {type(val).__name__})")
    # For nested [*] paths, count the minimum across groups so a single
    # under-perturbed parameter trips the floor.
    if "[*]" in floor.field:
        # val is the flattened collection across groups; we need the
        # minimum *per-group* length. Re-resolve to get per-group lists.
        head = floor.field.split("[*]")[0].rstrip(".")
        suffix = floor.field.split("[*]", 1)[1].lstrip(".")
        groups = _resolve_dotpath(doc, head) if head else doc
        if isinstance(groups, list) and suffix:
            per_group_lens: list[int] = []
            for g in groups:
                inner = _resolve_dotpath(g, suffix)
                if isinstance(inner, list):
                    per_group_lens.append(len(inner))
            if not per_group_lens:
                return False, None, (f"no group under {head!r} has a "
                                     f"{suffix!r} list")
            n = min(per_group_lens)
            return n >= floor.minimum, n, ""
    n = len(val)
    return n >= floor.minimum, n, ""


def _probe_yaml_field_truthy(run_dir: str,
                              floor: Floor) -> tuple[bool, int | None, str]:
    path = os.path.join(run_dir, floor.artifact)
    if not os.path.exists(path):
        return True, None, f"artifact {floor.artifact} absent"
    try:
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as e:
        return False, None, f"could not parse {floor.artifact}: {e}"
    val = _resolve_dotpath(doc, floor.field)
    truthy = bool(val)
    return truthy, (1 if truthy else 0), ""


def _probe_artifact_present(run_dir: str,
                            floor: Floor) -> tuple[bool, int | None, str]:
    """Phase 21 γ: the artifact must exist."""
    path = os.path.join(run_dir, floor.artifact)
    if os.path.exists(path):
        return True, 1, ""
    evidence = _calibration_evidence(run_dir) if \
        floor.triggered_by == "calibration_evidence" else None
    why = f" (evidence of fitting: {evidence})" if evidence else ""
    return False, 0, f"required artifact {floor.artifact} absent{why}"


# Suspiciously small literals on names that imply effort.
_SHORTCUT_LITERAL_NAMES = {
    "n_replicates": 5,
    "n_reps":       5,
    "nreps":        5,
    "n_runs":       5,
    "n_draws":      50,
    "ndraws":       50,
    "n_samples":    50,
    "maxiter":      100,
    "max_iter":     100,
    "n_iter":       100,
    "niter":        100,
    "iterations":   100,
    # Phase 21 γ: optimizer-budget names the Phase 19 docstring cited but
    # the table never listed.
    "n_trials":     20,
    "n_calls":      20,
    "max_evals":    50,
    "maxfev":       100,
    "max_nfev":     100,
    "n_restarts":   3,
}

# Replicate-count names that are legitimately 1 for deterministic models.
_DETERMINISTIC_OK_NAMES = frozenset({"n_replicates", "n_reps", "nreps",
                                     "n_runs"})

# Exception types whose `except X: pass` is an idiom, not an error swallow.
_BENIGN_PASS_EXCEPTIONS = frozenset({
    "ImportError", "ModuleNotFoundError", "FileExistsError",
})

# Single-word skip-pattern comment markers.
_SHORTCUT_COMMENT_RE = re.compile(r"#\s*(TODO|FIXME|HACK|XXX|SHORTCUT)\b",
                                    re.IGNORECASE)


# Phase 20 β: filenames/path-components that flag a file as test, smoke,
# or example code rather than production. Small literals in these files
# are intentional (`n_draws=5` in a smoke test, `maxiter=10` in an
# example) and must not fire shortcut HIGH.
_TEST_OR_EXAMPLE_FILENAME_RE = re.compile(
    r"(^|[/_-])(test|tests|smoke|example|examples|fixture|fixtures|"
    r"sandbox|scratch|notebook|notebooks)([/_-]|$|\.)",
    re.IGNORECASE,
)

# Phase 21 γ: the scan exemption is narrower than the classifier above.
# A file *named* `outcome_example.py` or `run_smoke.py` at models/ level
# is production-shaped and can be imported by outcome_fn.py, so only
# test-runner files and files inside a test/example *directory* are
# exempt — and every exemption is listed in the report, not silent.
_EXEMPT_DIR_RE = re.compile(
    r"^(test|tests|smoke|example|examples|fixture|fixtures|sandbox|"
    r"scratch|notebook|notebooks)$", re.IGNORECASE)
_EXEMPT_FILE_RE = re.compile(r"^(test_.*|.*_test|conftest)\.py$",
                             re.IGNORECASE)


def _is_test_or_example_path(path: str) -> bool:
    """True if any path component or filename signals test/smoke/
    example code. Catches `tests/foo.py`, `foo_test.py`, `test_foo.py`,
    `examples/foo.py`, `foo_smoke.py`, `foo_example.py`, etc.
    """
    parts = path.replace("\\", "/").split("/")
    if any(_TEST_OR_EXAMPLE_FILENAME_RE.search(p) for p in parts):
        return True
    return False


def _is_scan_exempt(relpath: str) -> bool:
    """Phase 21 γ: True for test-runner files and anything inside a
    test/example directory under models/."""
    parts = relpath.replace("\\", "/").split("/")
    if any(_EXEMPT_DIR_RE.match(p) for p in parts[:-1]):
        return True
    return bool(_EXEMPT_FILE_RE.match(parts[-1]))


def _comment_markers(src: str) -> list[tuple[int, str]]:
    """Phase 21 γ: TODO/FIXME markers in real comments only (tokenize),
    so "TODO" inside a string literal or docstring is not a finding."""
    import io
    import tokenize
    out: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                m = _SHORTCUT_COMMENT_RE.search(tok.string)
                if m:
                    out.append((tok.start[0], m.group(0)))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        for m in _SHORTCUT_COMMENT_RE.finditer(src):
            out.append((src[:m.start()].count("\n") + 1, m.group(0)))
    return out


def _small_int(node: ast.AST, consts: dict[str, int]) -> int | None:
    """Integer value of a literal or of a module-level int constant."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int) \
            and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    return None


def _target_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _scan_source(src: str, rel: str) -> list[str]:
    findings: list[str] = []
    for line, marker in _comment_markers(src):
        findings.append(f"{rel}:{line}: {marker}")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return findings
    deterministic = "deterministic" in src.lower()

    # Module-level integer constants, for `N = 5; n_draws = N` indirection.
    consts: dict[str, int] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                and isinstance(stmt.targets[0], ast.Name):
            v = _small_int(stmt.value, {})
            if v is not None:
                consts[stmt.targets[0].id] = v
        elif isinstance(stmt, ast.AnnAssign) and \
                isinstance(stmt.target, ast.Name) and stmt.value is not None:
            v = _small_int(stmt.value, {})
            if v is not None:
                consts[stmt.target.id] = v

    seen: set[tuple[int, str]] = set()

    def check(name: str | None, value_node: ast.AST | None, lineno: int,
              sep: str) -> None:
        if not name or value_node is None:
            return
        key = name.lower()
        if key not in _SHORTCUT_LITERAL_NAMES:
            return
        v = _small_int(value_node, consts)
        if v is None:
            return
        threshold = _SHORTCUT_LITERAL_NAMES[key]
        if v >= threshold:
            return
        if deterministic and v == 1 and key in _DETERMINISTIC_OK_NAMES:
            return
        if (lineno, key) in seen:
            return
        seen.add((lineno, key))
        via = (f" (via {value_node.id})"
               if isinstance(value_node, ast.Name) else "")
        findings.append(f"{rel}:{lineno}: {name}{sep}{v}{via} below "
                        f"shortcut threshold {threshold}")

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                names: list[str] = []
                t = node.type
                for elt in (t.elts if isinstance(t, ast.Tuple) else [t]):
                    if isinstance(elt, ast.Name):
                        names.append(elt.id)
                    elif isinstance(elt, ast.Attribute):
                        names.append(elt.attr)
                if not (names and all(n in _BENIGN_PASS_EXCEPTIONS
                                      for n in names)):
                    findings.append(f"{rel}:{node.lineno}: bare except: "
                                    f"pass swallows errors")
        elif isinstance(node, ast.keyword):
            check(node.arg, node.value, getattr(node.value, "lineno", 0), "=")
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                check(_target_name(tgt), node.value, node.lineno, " = ")
        elif isinstance(node, ast.AnnAssign):
            check(_target_name(node.target), node.value, node.lineno, " = ")
        elif isinstance(node, ast.Dict):
            # options={"maxiter": 10} (scipy), config={"n_trials": 5}
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    check(k.value, v, node.lineno, ": ")
    return findings


def _probe_ast_shortcut_scan(run_dir: str,
                              floor: Floor) -> tuple[bool, int | None, str, list[str]]:
    """AST + token scan over *.py under models/. Returns
    (passes, count, diagnostic, file_findings).

    Catches direct kwargs (`optimize(n_trials=5)`), name / attribute /
    annotated assignments (`n_draws = 5`, `self.n_draws = 5`,
    `n_draws: int = 5`), dict-config keys (`options={"maxiter": 10}`),
    and one level of module-constant indirection (`N = 5; n_draws = N`).
    Still best-effort: values read from CLI/env/files are invisible. It
    is a tripwire for left-behind shortcuts, not a sandbox.
    """
    base = os.path.join(run_dir, floor.artifact.rstrip("/"))
    if not os.path.isdir(base):
        return True, None, f"{floor.artifact} not a directory", []
    findings: list[str] = []
    exempt: list[str] = []
    for root, _, files in os.walk(base):
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(root, fname)
            if _is_scan_exempt(os.path.relpath(path, base)):
                exempt.append(os.path.relpath(path, run_dir))
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    src = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            findings.extend(_scan_source(src, os.path.relpath(path, run_dir)))
    n = len(findings)
    diag = (f"exempt (test/example) files not scanned: {', '.join(exempt)}"
            if exempt else "")
    return n <= floor.minimum, n, diag, findings


# --------------------------------------------------------------------------- #
# Top-level: evaluate
# --------------------------------------------------------------------------- #


@dataclass
class Violation:
    floor_id: str
    severity: str
    observed: int | None
    minimum: int
    scope_declarable: bool
    diagnostic: str
    findings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "floor_id": self.floor_id,
            "severity": self.severity,
            "observed": self.observed,
            "minimum": self.minimum,
            "scope_declarable": self.scope_declarable,
            "diagnostic": self.diagnostic,
        }
        if self.findings:
            d["findings"] = list(self.findings)
        return d


def evaluate(run_dir: str,
             manifest_path: str | None = None) -> dict:
    """Run all floor probes against run_dir; return a report dict."""
    defaults, floors = load_manifest(manifest_path)
    violations: list[Violation] = []
    probed: list[dict] = []
    for floor in floors:
        if not _trigger_fires(floor.triggered_by, run_dir):
            probed.append({
                "floor_id": floor.id,
                "status": "skipped_trigger",
                "trigger": floor.triggered_by,
            })
            continue
        if floor.probe == "yaml_field":
            passes, obs, diag = _probe_yaml_field(run_dir, floor)
            findings: list[str] = []
        elif floor.probe == "yaml_field_list_length":
            passes, obs, diag = _probe_yaml_field_list_length(run_dir, floor)
            findings = []
        elif floor.probe == "yaml_field_truthy":
            passes, obs, diag = _probe_yaml_field_truthy(run_dir, floor)
            findings = []
        elif floor.probe == "artifact_present":
            passes, obs, diag = _probe_artifact_present(run_dir, floor)
            findings = []
        elif floor.probe == "ast_shortcut_scan":
            passes, obs, diag, findings = _probe_ast_shortcut_scan(run_dir, floor)
        else:
            continue
        probed.append({
            "floor_id": floor.id,
            "status": "passed" if passes else "violated",
            "observed": obs,
            "minimum": floor.minimum,
        })
        if not passes:
            violations.append(Violation(
                floor_id=floor.id,
                severity=floor.severity,
                observed=obs,
                minimum=floor.minimum,
                scope_declarable=floor.scope_declarable,
                diagnostic=diag or "",
                findings=findings,
            ))
    return {
        "manifest": manifest_path or _MANIFEST_PATH,
        "defaults": dict(defaults),
        "probed": probed,
        "violations": [v.to_dict() for v in violations],
        "n_high": sum(1 for v in violations if v.severity == "HIGH"),
        "n_medium": sum(1 for v in violations if v.severity == "MEDIUM"),
    }


def write_report(run_dir: str, report: dict) -> str:
    out_path = os.path.join(run_dir, "effort_floors_report.yaml")
    with open(out_path, "w") as f:
        yaml.safe_dump(report, f, sort_keys=False)
    return out_path


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #


def _self_test() -> int:
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    # T1: manifest loads cleanly.
    defaults, floors = load_manifest()
    ok(len(floors) >= 7, f"T1: expected ≥ 7 floors, got {len(floors)}")
    ok(any(f.id == "uq_min_draws_local" for f in floors),
       "T1: uq_min_draws_local must be present")

    # T2: malformed manifest is rejected.
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("floors: not_a_list\n")
        bad = f.name
    try:
        try:
            load_manifest(bad)
            ok(False, "T2: malformed manifest must raise")
        except ValueError:
            pass
    finally:
        os.unlink(bad)

    # T3: yaml_field probe — under-floor n_draws fires HIGH violation.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "uncertainty_report.yaml"), "w") as f:
            yaml.safe_dump({"n_draws": 50, "scalar_outputs": {}}, f)
        rep = evaluate(d)
        viols = [v for v in rep["violations"] if v["floor_id"] == "uq_min_draws_local"]
        ok(len(viols) == 1 and viols[0]["severity"] == "HIGH"
           and viols[0]["observed"] == 50,
           f"T3: 50 draws should fire HIGH, got {viols}")

    # T4: yaml_field probe — at-floor n_draws does not fire.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "uncertainty_report.yaml"), "w") as f:
            yaml.safe_dump({"n_draws": 200, "scalar_outputs": {}}, f)
        rep = evaluate(d)
        viols = [v for v in rep["violations"] if v["floor_id"] == "uq_min_draws_local"]
        ok(not viols, f"T4: 200 draws should pass, got {viols}")

    # T5: trigger absent → probe skipped (no violations from uq floor).
    with tempfile.TemporaryDirectory() as d:
        rep = evaluate(d)
        viols = [v for v in rep["violations"] if v["floor_id"] == "uq_min_draws_local"]
        ok(not viols,
           f"T5: missing uncertainty_report.yaml must skip uq probe, got {viols}")
        skipped = [p for p in rep["probed"]
                   if p["floor_id"] == "uq_min_draws_local"
                   and p["status"] == "skipped_trigger"]
        ok(len(skipped) == 1,
           f"T5: probed log must record skipped_trigger, got {rep['probed']}")

    # T6: list-length probe — under-floor perturbation count fires HIGH per-group.
    # Phase 21 γ: fixtures use the canonical sensitivity_analysis.py
    # schema (`load_bearing_parameters`); the Phase 19 fixtures used a
    # `parameters` key no real artifact has, which is why the floor's
    # wrong path passed its own tests.
    def _sa_param(pid: str, n_pert: int) -> dict:
        return {"id": pid, "parameter": pid, "primary_value": 0.5,
                "primary_objective": 100.0,
                "perturbations": [
                    {"value": 0.3 + 0.1 * j, "objective": 99.0,
                     "rank_change_top_n": 0,
                     "primary_recommendation_changes": False}
                    for j in range(n_pert)]}

    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "sensitivity_analysis.yaml"), "w") as f:
            yaml.safe_dump({
                "load_bearing_parameters": [
                    _sa_param("vaccine_efficacy", 2),
                    _sa_param("contact_rate", 1),  # under-floor
                ],
                "verdict": "ROBUST",
            }, f)
        rep = evaluate(d)
        viols = [v for v in rep["violations"]
                 if v["floor_id"] == "sensitivity_min_perturbation_points"]
        ok(len(viols) == 1 and viols[0]["observed"] == 1,
           f"T6: min-per-group should report observed=1, got {viols}")

    # T7: list-length probe — pass when all groups meet floor.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "sensitivity_analysis.yaml"), "w") as f:
            yaml.safe_dump({
                "load_bearing_parameters": [
                    _sa_param(f"p{i}", 2) for i in range(6)
                ],
                "verdict": "ROBUST",
            }, f)
        rep = evaluate(d)
        viols = [v for v in rep["violations"]
                 if v["floor_id"] in ("sensitivity_min_perturbation_points",
                                       "sensitivity_min_params_perturbed")]
        ok(not viols, f"T7: all-pass sensitivity should be silent, got {viols}")

    # T8: yaml_field_truthy — held_out_fold missing/None fires HIGH.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "calibration_result.yaml"), "w") as f:
            yaml.safe_dump({
                "n_restarts": 3, "n_iterations": 1000,
                "held_out_fold": None,
            }, f)
        rep = evaluate(d)
        viols = [v for v in rep["violations"]
                 if v["floor_id"] == "calibration_held_out"]
        ok(len(viols) == 1 and viols[0]["severity"] == "HIGH",
           f"T8: held_out_fold None should fire HIGH, got {viols}")

    # T9: yaml_field_truthy — held_out_fold present passes; under-restarts fires.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "calibration_result.yaml"), "w") as f:
            yaml.safe_dump({
                "n_restarts": 1, "n_iterations": 1000,
                "held_out_fold": {"indices": [3, 5, 7]},
            }, f)
        rep = evaluate(d)
        held = [v for v in rep["violations"]
                if v["floor_id"] == "calibration_held_out"]
        restarts = [v for v in rep["violations"]
                    if v["floor_id"] == "calibration_min_restarts"]
        ok(not held, f"T9: held_out_fold truthy must pass, got {held}")
        ok(len(restarts) == 1 and restarts[0]["observed"] == 1,
           f"T9: n_restarts=1 must fire HIGH, got {restarts}")

    # T10: AST shortcut scan — finds n_replicates=1 and TODO comment.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "model_run.py"), "w") as f:
            f.write(
                "# TODO: increase replicates before final run\n"
                "import sciris as sc\n"
                "def run():\n"
                "    n_replicates = 1\n"
                "    return sc.objdict(n=n_replicates)\n"
                "try:\n"
                "    run()\n"
                "except Exception:\n"
                "    pass\n"
            )
        rep = evaluate(d)
        viols = [v for v in rep["violations"]
                 if v["floor_id"] == "shortcut_markers_in_model_code"]
        ok(len(viols) == 1, f"T10: shortcut scan should fire once, got {viols}")
        # Three findings: TODO comment, n_replicates=1, bare except pass
        ok(viols[0]["observed"] >= 3,
           f"T10: expected ≥3 findings, got {viols[0]['observed']}")
        assert "findings" in viols[0]
        joined = " | ".join(viols[0]["findings"])
        ok("TODO" in joined and "n_replicates" in joined and "except" in joined,
           f"T10: findings must cover all three markers, got {joined}")

    # T11: AST shortcut scan — clean model code is silent.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "model_run.py"), "w") as f:
            f.write(
                "def run():\n"
                "    n_replicates = 50\n"
                "    return n_replicates\n"
            )
        rep = evaluate(d)
        viols = [v for v in rep["violations"]
                 if v["floor_id"] == "shortcut_markers_in_model_code"]
        ok(not viols, f"T11: clean code must be silent, got {viols}")

    # T12: write_report writes parseable YAML
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "uncertainty_report.yaml"), "w") as f:
            yaml.safe_dump({"n_draws": 50}, f)
        rep = evaluate(d)
        path = write_report(d, rep)
        with open(path) as f:
            loaded = yaml.safe_load(f)
        ok(loaded.get("n_high", 0) >= 1,
           f"T12: report must record HIGH count, got {loaded}")

    # T13: cloud_uq trigger fires only when `cloud:` or `n_nodes:` is set.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "uncertainty_report.yaml"), "w") as f:
            yaml.safe_dump({"n_draws": 500, "cloud": {"n_nodes": 4}}, f)
        rep = evaluate(d)
        viols = [v for v in rep["violations"]
                 if v["floor_id"] == "uq_min_draws_cloud"]
        # 500 < 1000 floor → fires MEDIUM
        ok(len(viols) == 1 and viols[0]["severity"] == "MEDIUM",
           f"T13: cloud UQ at 500 draws should fire MEDIUM, got {viols}")

    # T14 (Phase 20 β): AST shortcut scan skips test/smoke/example
    # files. Smoke-test draw counts and example max_iter ceilings are
    # intentional small literals; firing on them poisons the budget.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        # Production file: should fire
        with open(os.path.join(d, "models", "model_run.py"), "w") as f:
            f.write("def run():\n    n_replicates = 1\n    return n_replicates\n")
        # Test file: should be skipped
        with open(os.path.join(d, "models", "test_smoke.py"), "w") as f:
            f.write("def test_one():\n    n_draws = 5\n    assert n_draws\n")
        # Examples subdir: should be skipped
        os.makedirs(os.path.join(d, "models", "examples"))
        with open(os.path.join(d, "models", "examples", "demo.py"), "w") as f:
            f.write("# TODO: extend example\nn_iter = 10\n")
        rep = evaluate(d)
        viols = [v for v in rep["violations"]
                 if v["floor_id"] == "shortcut_markers_in_model_code"]
        joined = " | ".join(viols[0]["findings"]) if viols else ""
        ok(len(viols) == 1, f"T14: production shortcut must fire, got {viols}")
        ok("model_run.py" in joined and "n_replicates" in joined,
           f"T14: production finding must be present, got {joined}")
        ok("test_smoke.py" not in joined,
           f"T14: test_smoke.py must be skipped, got {joined}")
        ok("examples/demo.py" not in joined and "demo.py" not in joined,
           f"T14: examples/ must be skipped, got {joined}")

    # T14b (Phase 20 β): truthy-but-not-a-list `held_out_fold` placeholder
    # (e.g. `"yes"`, a bare number) no longer satisfies the floor.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "calibration_result.yaml"), "w") as f:
            yaml.safe_dump({
                "n_restarts": 3, "n_iterations": 1000,
                "held_out_fold": "yes",
            }, f)
        rep = evaluate(d)
        viols = [v for v in rep["violations"]
                 if v["floor_id"] == "calibration_held_out"]
        ok(len(viols) == 1 and viols[0]["severity"] == "HIGH",
           f"T14b: placeholder string must fail HIGH, got {viols}")

    # T14c (Phase 20 β): empty indices list also fails the floor.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "calibration_result.yaml"), "w") as f:
            yaml.safe_dump({
                "n_restarts": 3, "n_iterations": 1000,
                "held_out_fold": {"indices": []},
            }, f)
        rep = evaluate(d)
        viols = [v for v in rep["violations"]
                 if v["floor_id"] == "calibration_held_out"]
        ok(len(viols) == 1 and viols[0]["severity"] == "HIGH",
           f"T14c: empty indices list must fail HIGH, got {viols}")

    # T15 (Phase 20 β): `_is_test_or_example_path` recognizes the
    # common variants without spuriously matching production names.
    cases = [
        ("test_outcome.py", True),
        ("outcome_test.py", True),
        ("tests/foo.py", True),
        ("smoke/run.py", True),
        ("run_smoke.py", True),
        ("examples/foo.py", True),
        ("notebooks/explore.py", True),
        ("scratch/quick.py", True),
        ("model_run.py", False),
        ("outcome_fn.py", False),
        ("estimate.py", False),
        ("contestable.py", False),
    ]
    for relpath, expected in cases:
        got = _is_test_or_example_path(relpath)
        ok(got is expected,
           f"T15: _is_test_or_example_path({relpath!r}) → {got}, expected {expected}")

    # ---- Phase 21 γ cases -------------------------------------------- #

    # T16: fitted parameters exist (identifiability manifest) but no
    # calibration_result.yaml → HIGH non-declarable calibration_result_missing.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "identifiability.yaml"), "w") as f:
            yaml.safe_dump({"parameters": [{"name": "beta"}]}, f)
        rep = evaluate(d)
        viols = [v for v in rep["violations"]
                 if v["floor_id"] == "calibration_result_missing"]
        ok(len(viols) == 1 and viols[0]["severity"] == "HIGH"
           and viols[0]["scope_declarable"] is False,
           f"T16: missing calibration_result must fire HIGH, got {viols}")

    # T16b: no evidence of fitting → no calibration_result_missing.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        rep = evaluate(d)
        viols = [v for v in rep["violations"]
                 if v["floor_id"] == "calibration_result_missing"]
        ok(not viols, f"T16b: no fitting evidence must be silent, got {viols}")

    # T16c: model_comparison with k_params > 0 is evidence too.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "model_comparison.yaml"), "w") as f:
            yaml.safe_dump({"models": [{"name": "null", "k_params": 0},
                                       {"name": "seir", "k_params": 4}]}, f)
        rep = evaluate(d)
        ok(any(v["floor_id"] == "calibration_result_missing"
               for v in rep["violations"]),
           "T16c: k_params>0 in model_comparison must trigger")

    # T17: failed draws are subtracted (200 requested, 39 errors → 161).
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "uncertainty_report.yaml"), "w") as f:
            yaml.safe_dump({"n_draws": 200, "n_errors": 39}, f)
        rep = evaluate(d)
        viols = [v for v in rep["violations"]
                 if v["floor_id"] == "uq_min_draws_local"]
        ok(len(viols) == 1 and viols[0]["observed"] == 161,
           f"T17: 161 effective draws must fire, got {viols}")
    # T17b: explicit n_successful wins and is not double-subtracted.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "uncertainty_report.yaml"), "w") as f:
            yaml.safe_dump({"n_draws": 250, "n_successful": 210,
                            "n_errors": 40}, f)
        rep = evaluate(d)
        ok(not [v for v in rep["violations"]
                if v["floor_id"] == "uq_min_draws_local"],
           "T17b: n_successful=210 must pass")

    # T17c: propagate_uncertainty's Phase 21 schema (n_draws_requested,
    # n_successful, n_failed) — n_successful is used directly.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "uncertainty_report.yaml"), "w") as f:
            yaml.safe_dump({"n_draws": 200, "n_draws_requested": 200,
                            "n_successful": 170, "n_failed": 30,
                            "backend": "local"}, f)
        rep = evaluate(d)
        viols = [v for v in rep["violations"]
                 if v["floor_id"] == "uq_min_draws_local"]
        ok(len(viols) == 1 and viols[0]["observed"] == 170,
           f"T17c: n_successful=170 must fire, got {viols}")
    # T17d: no n_successful but n_failed present → subtracted.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "uncertainty_report.yaml"), "w") as f:
            yaml.safe_dump({"n_draws": 210, "n_failed": 20}, f)
        rep = evaluate(d)
        viols = [v for v in rep["violations"]
                 if v["floor_id"] == "uq_min_draws_local"]
        ok(len(viols) == 1 and viols[0]["observed"] == 190,
           f"T17d: 210-20=190 must fire, got {viols}")

    # T18: `backend: cloud` (propagate_uncertainty) triggers the cloud floor.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "uncertainty_report.yaml"), "w") as f:
            yaml.safe_dump({"n_draws": 400, "n_errors": 0,
                            "backend": "cloud"}, f)
        rep = evaluate(d)
        ok(any(v["floor_id"] == "uq_min_draws_cloud"
               for v in rep["violations"]),
           "T18: backend=cloud at 400 draws must fire uq_min_draws_cloud")
    # T18b: local backend does not.
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "uncertainty_report.yaml"), "w") as f:
            yaml.safe_dump({"n_draws": 400, "backend": "local"}, f)
        rep = evaluate(d)
        ok(not any(v["floor_id"] == "uq_min_draws_cloud"
                   for v in rep["violations"]),
           "T18b: local backend must not trigger cloud floor")

    # T19: AST bypasses the Phase 19/20 scan missed are now caught.
    bypass_src = (
        "from scipy.optimize import minimize\n"
        "N_DRAWS = 5\n"
        "class M:\n"
        "    def __init__(self):\n"
        "        self.n_draws = 5\n"
        "def fit(f, x0, study):\n"
        "    n_draws = N_DRAWS\n"
        "    n_samples: int = 10\n"
        "    minimize(f, x0, options={'maxiter': 10})\n"
        "    study.optimize(f, n_trials=5)\n"
    )
    hits = _scan_source(bypass_src, "models/fit.py")
    joined = " | ".join(hits)
    for needle in ("N_DRAWS = 5", "via N_DRAWS", "n_draws = 5",
                   "n_samples = 10", "maxiter: 10", "n_trials=5"):
        ok(needle in joined, f"T19: expected {needle!r} in findings, got {joined}")

    # T20: idioms that are not shortcuts stay silent.
    benign_src = (
        '"""Deterministic ODE model: one run is exact."""\n'
        "import os\n"
        "try:\n"
        "    import numba\n"
        "except ImportError:\n"
        "    pass\n"
        "try:\n"
        "    os.makedirs('out')\n"
        "except FileExistsError:\n"
        "    pass\n"
        "NOTE = 'TODO list lives in the plan, not here'\n"
        "n_runs = 1\n"
    )
    hits = _scan_source(benign_src, "models/ode.py")
    ok(not hits, f"T20: benign idioms must be silent, got {hits}")
    # T20b: but `except Exception: pass` and a real TODO comment still fire.
    hits = _scan_source("try:\n    x()\nexcept Exception:\n    pass\n"
                        "# TODO: tune\n", "models/m.py")
    ok(len(hits) == 2, f"T20b: expected 2 findings, got {hits}")

    # T21: production-shaped `*_example.py` at models/ level is scanned;
    # test files and test/example dirs are exempt but listed in the
    # diagnostic (not silent).
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models", "tests"))
        with open(os.path.join(d, "models", "outcome_example.py"), "w") as f:
            f.write("n_draws = 5\n")
        with open(os.path.join(d, "models", "tests", "test_x.py"), "w") as f:
            f.write("n_draws = 5\n")
        rep = evaluate(d)
        viols = [v for v in rep["violations"]
                 if v["floor_id"] == "shortcut_markers_in_model_code"]
        ok(len(viols) == 1 and viols[0]["observed"] == 1
           and "outcome_example.py" in " ".join(viols[0]["findings"]),
           f"T21: models/outcome_example.py must be scanned, got {viols}")
        ok(bool(viols) and "tests/test_x.py" in viols[0]["diagnostic"],
           f"T21: exempt file must be listed, got {viols}")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", nargs="?")
    p.add_argument("--manifest", default=None,
                   help="Override path to effort_floors.yaml")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--json", action="store_true",
                   help="Emit the report as JSON on stdout in addition "
                        "to writing effort_floors_report.yaml")
    args = p.parse_args()
    if args.self_test:
        return _self_test()
    if not args.run_dir:
        p.error("run_dir is required (or use --self-test)")
    if not os.path.isdir(args.run_dir):
        print(f"ERROR: {args.run_dir} is not a directory", file=sys.stderr)
        return 2
    try:
        rep = evaluate(args.run_dir, manifest_path=args.manifest)
    except (ValueError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    write_report(args.run_dir, rep)
    if args.json:
        import json
        print(json.dumps(rep, indent=2, default=str))
    n_high = rep["n_high"]
    n_med = rep["n_medium"]
    print(f"effort_floors: {n_high} HIGH, {n_med} MEDIUM violation(s) "
          f"-> {args.run_dir}/effort_floors_report.yaml", file=sys.stderr)
    return 1 if (n_high + n_med) else 0


if __name__ == "__main__":
    sys.exit(main())
