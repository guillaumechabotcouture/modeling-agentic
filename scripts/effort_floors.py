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
ast_shortcut_scan       Walk *.py under models/, scan AST for shortcut markers
                        (TODO/FIXME/HACK comments, bare `except: pass`, suspiciously
                        small literals on n_replicates/n_draws/maxiter/n_iter).

The script is intentionally tolerant: a missing trigger artifact (e.g.,
no uncertainty_report.yaml means UQ was never run) skips the probe rather
than firing. The validator decides separately whether absence of UQ is
itself a violation — that's the job of Phase 2's rigor-artifact check,
not this script.

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

_MANIFEST_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..",
                 ".claude", "orchestration", "effort_floors.yaml")
)

_VALID_SEVERITIES = frozenset({"HIGH", "MEDIUM"})
_VALID_PROBES = frozenset({
    "yaml_field", "yaml_field_list_length",
    "yaml_field_truthy", "ast_shortcut_scan",
})
_VALID_TRIGGERS = frozenset({
    "always", "uq_present", "cloud_uq_present",
    "sensitivity_present", "calibration_present",
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
        # cloud_batch annotates with a `cloud:` block or `n_nodes`
        return bool(doc.get("cloud") or doc.get("n_nodes"))
    if trigger == "sensitivity_present":
        return os.path.exists(
            os.path.join(run_dir, "models", "sensitivity_analysis.yaml"))
    if trigger == "calibration_present":
        return os.path.exists(
            os.path.join(run_dir, "models", "calibration_result.yaml"))
    return False


# --------------------------------------------------------------------------- #
# Probes
# --------------------------------------------------------------------------- #


def _resolve_dotpath(doc: Any, path: str) -> Any:
    """Resolve a dotted path through a nested dict/list.

    Supports `[*]` to mean "iterate this list and collect all leaf values"
    so list-length probes can target nested lists like
    `parameters[*].perturbations`.
    """
    parts = path.split(".")
    cur: Any = doc
    for part in parts:
        if cur is None:
            return None
        # Handle [*] iteration
        if "[*]" in part:
            head = part.replace("[*]", "")
            if head:
                if not isinstance(cur, dict) or head not in cur:
                    return None
                cur = cur[head]
            if not isinstance(cur, list):
                return None
            # Continue with the remaining suffix on each element, then flatten.
            remaining = ".".join(parts[parts.index(part) + 1:])
            if not remaining:
                return cur
            collected = []
            for elem in cur:
                sub = _resolve_dotpath(elem, remaining)
                if isinstance(sub, list):
                    collected.extend(sub)
                elif sub is not None:
                    collected.append(sub)
            return collected
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


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
    val = _resolve_dotpath(doc, floor.field)
    if val is None:
        return False, None, (f"field {floor.field!r} absent in "
                             f"{floor.artifact}; floor expects ≥ {floor.minimum}")
    try:
        n = int(val)
    except (TypeError, ValueError):
        return False, None, (f"field {floor.field!r} in {floor.artifact} "
                             f"is not an integer (got {val!r})")
    return n >= floor.minimum, n, ""


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
}

# Single-word skip-pattern comment markers.
_SHORTCUT_COMMENT_RE = re.compile(r"#\s*(TODO|FIXME|HACK|XXX|SHORTCUT)\b",
                                    re.IGNORECASE)


# Phase 20 β: filenames/path-components that flag a file as test, smoke,
# or example code rather than production. Small literals in these files
# are intentional (`n_draws=5` in a smoke test, `maxiter=10` in an
# example) and must not fire shortcut HIGH. The exclusion is best-effort
# — anyone who writes a production module called `test_outcomes.py`
# evades the scan, but that's an acceptable trade for not blocking real
# Phase 19 runs on legitimate test code.
_TEST_OR_EXAMPLE_FILENAME_RE = re.compile(
    r"(^|[/_-])(test|tests|smoke|example|examples|fixture|fixtures|"
    r"sandbox|scratch|notebook|notebooks)([/_-]|$|\.)",
    re.IGNORECASE,
)


def _is_test_or_example_path(path: str) -> bool:
    """True if any path component or filename signals test/smoke/
    example code. Catches `tests/foo.py`, `foo_test.py`, `test_foo.py`,
    `examples/foo.py`, `foo_smoke.py`, `foo_example.py`, etc.
    """
    parts = path.replace("\\", "/").split("/")
    if any(_TEST_OR_EXAMPLE_FILENAME_RE.search(p) for p in parts):
        return True
    return False


def _probe_ast_shortcut_scan(run_dir: str,
                              floor: Floor) -> tuple[bool, int | None, str, list[str]]:
    """AST + textual scan over *.py under models/. Returns
    (passes, count, diagnostic, file_findings).

    Phase 20 β scope notes:
    - Skips test/smoke/example files (see `_is_test_or_example_path`).
    - The scan is **best-effort**. It catches direct keyword arguments
      (`optimize(n_trials=5)`) and direct name assignments
      (`n_draws = 5`), but it does NOT catch dict-config patterns
      (`config = {"n_trials": 5}; optimize(**config)`), attribute-set
      shortcuts (`cfg.n_trials = 5`), or values read from CLI/env. A
      modeler determined to evade the scan can always do so; the floor
      is a tripwire for the casual `n_draws=5` left-behind, not a
      sandbox.
    """
    base = os.path.join(run_dir, floor.artifact.rstrip("/"))
    if not os.path.isdir(base):
        return True, None, f"{floor.artifact} not a directory", []
    findings: list[str] = []
    for root, _, files in os.walk(base):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(root, fname)
            # Phase 20 β: skip test/smoke/example files. Small literals
            # in these files are intentional (smoke-test draws, example
            # max_iter ceilings) and would otherwise burn the shortcut
            # budget toward HIGH at round ≥ 2.
            if _is_test_or_example_path(os.path.relpath(path, base)):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    src = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            # Textual: shortcut comments
            for m in _SHORTCUT_COMMENT_RE.finditer(src):
                line = src[:m.start()].count("\n") + 1
                findings.append(
                    f"{os.path.relpath(path, run_dir)}:{line}: {m.group(0)}"
                )
            # AST: small literals on shortcut names + bare except: pass
            try:
                tree = ast.parse(src, filename=path)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                # Bare exception swallow: `except ...: pass`
                if isinstance(node, ast.ExceptHandler):
                    if (len(node.body) == 1
                            and isinstance(node.body[0], ast.Pass)):
                        findings.append(
                            f"{os.path.relpath(path, run_dir)}:"
                            f"{node.lineno}: bare except: pass swallows errors"
                        )
                # Keyword argument with small literal on a shortcut name:
                # foo(n_replicates=1)
                if isinstance(node, ast.keyword) and node.arg in _SHORTCUT_LITERAL_NAMES:
                    if isinstance(node.value, ast.Constant) and \
                            isinstance(node.value.value, int):
                        threshold = _SHORTCUT_LITERAL_NAMES[node.arg]
                        if node.value.value < threshold:
                            findings.append(
                                f"{os.path.relpath(path, run_dir)}:"
                                f"{node.lineno}: {node.arg}="
                                f"{node.value.value} below shortcut "
                                f"threshold {threshold}"
                            )
                # Direct assignment: n_replicates = 1
                if isinstance(node, ast.Assign):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name) and tgt.id in _SHORTCUT_LITERAL_NAMES:
                            if isinstance(node.value, ast.Constant) and \
                                    isinstance(node.value.value, int):
                                threshold = _SHORTCUT_LITERAL_NAMES[tgt.id]
                                if node.value.value < threshold:
                                    findings.append(
                                        f"{os.path.relpath(path, run_dir)}:"
                                        f"{node.lineno}: {tgt.id} = "
                                        f"{node.value.value} below "
                                        f"shortcut threshold {threshold}"
                                    )
    n = len(findings)
    return n <= floor.minimum, n, "", findings


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
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "sensitivity_analysis.yaml"), "w") as f:
            yaml.safe_dump({
                "parameters": [
                    {"name": "or_pbo", "perturbations": [0.5, 0.7, 0.9]},
                    {"name": "or_irs", "perturbations": [0.6]},  # under-floor
                ]
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
                "parameters": [
                    {"name": f"p{i}", "perturbations": [0.5, 0.7, 0.9]}
                    for i in range(6)
                ]
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
