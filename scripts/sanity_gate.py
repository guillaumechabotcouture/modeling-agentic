#!/usr/bin/env python3
"""Sanity-schema check (STAGE 7 gate check, Phase 13 α).

    python3 scripts/sanity_gate.py <run_dir> [--round N]
    python3 scripts/sanity_gate.py --self-test

Phase 48 β moved this check out of scripts/validate_critique_yaml.py
unchanged. `gate_findings(run_dir, round_n)` runs scripts/sanity_checks.py on
`models/sanity_schema.yaml` (required from round 3) and returns one MEDIUM per
failed check not acknowledged in scope_declaration.yaml. The validator runs it with `run_guarded` under the old
name `_check_sanity_schema`, at the same place in its check order as before, so a
crash finding reads as before.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:   # loaded by the validator, scripts/ is already there
    sys.path.insert(0, HERE)
from lib.rigor_artifacts import artifact_path  # noqa: E402

# Phase 13 Commit α: disease-agnostic structural sanity checks.
#
# `models/sanity_schema.yaml` declares abstract slots (outcome,
# exposure, shares, derived-consistency formulas, composite-dimension
# windows, counterfactual ratio, structural-uncertainty carry-
# forward, outlier sniff). `scripts/sanity_checks.py` runs eight
# generic structural checks against the schema. The schema is
# required when the modeler has had time to compute headline outputs
# (round ≥ 3). Each failed check emits a MEDIUM advisory; none block
# ACCEPT. The modeler can opt out of any check via
# `scope_declaration.yaml`'s `sanity_check_acknowledged: <id>` list.
_SANITY_SCHEMA_REQUIRED_FROM_ROUND = 3


def gate_findings(run_dir: str,
                  round_n: int | None = None) -> list[dict]:
    """Phase 13 Commit α: validate models/sanity_schema.yaml.

    Emits:
      sanity_schema_missing               MEDIUM — yaml absent at r≥3
      sanity_schema_invalid               HIGH   — yaml malformed
      sanity_check_failed_<id>            MEDIUM — one per failed check
    """
    if round_n is not None and round_n < _SANITY_SCHEMA_REQUIRED_FROM_ROUND:
        return []  # Too early; modeler still composing outputs.

    yaml_path = artifact_path("sanity_schema", run_dir)
    if not os.path.exists(yaml_path):
        if round_n is None:
            return []  # Round unknown; defer.
        return [{
            "kind": "sanity_schema_missing",
            "severity": "MEDIUM",
            "stage": "MODEL",
            "claim": (
                f"Required artifact `models/sanity_schema.yaml` is "
                f"absent at round {round_n} (≥ "
                f"{_SANITY_SCHEMA_REQUIRED_FROM_ROUND}). The modeler "
                f"must declare abstract slots (outcome name, baseline "
                f"reservoir, exposure unit, shares, derived-consistency "
                f"formulas, composite-dimension windows, structural-"
                f"uncertainty bounds) so eight disease-agnostic "
                f"structural checks can run. See the `sanity-schema` "
                f"skill and `scripts/sanity_checks.py --self-test` for "
                f"the schema."
            ),
        }]

    try:
        spec = importlib.util.spec_from_file_location(
            "sanity_checks",
            os.path.join(os.path.dirname(__file__), "sanity_checks.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:
        return [{
            "kind": "sanity_schema_invalid",
            "severity": "HIGH",
            "stage": "MODEL",
            "claim": f"Could not load scripts/sanity_checks.py: {e}",
        }]

    result = mod.validate_sanity_schema(yaml_path, run_dir=run_dir)
    if result["verdict"] == "MALFORMED":
        return [{
            "kind": "sanity_schema_invalid",
            "severity": "HIGH",
            "stage": "MODEL",
            "claim": (f"models/sanity_schema.yaml is malformed: "
                      f"{'; '.join(result.get('errors') or ['(no detail)'])}"),
        }]

    # Honor explicit opt-outs from scope_declaration.yaml.
    acknowledged = _load_sanity_check_acknowledged(run_dir)

    out: list[dict] = []
    for c in result.get("checks", []):
        if c.get("passed"):
            continue
        check_id = c.get("id", "unknown")
        if check_id in acknowledged:
            continue
        out.append({
            "kind": f"sanity_check_failed_{check_id}",
            "severity": "MEDIUM",
            "stage": "MODEL",
            "key": check_id,  # Phase 47 ε: instance key for scripts/stall_rule.py
            "claim": (
                f"Sanity check {check_id!r} failed: {c.get('claim', '')}. "
                f"Either fix the underlying numbers/relationships in "
                f"models/sanity_schema.yaml or acknowledge with "
                f"`sanity_check_acknowledged: [{check_id}]` in "
                f"scope_declaration.yaml (with rationale)."
            ),
        })
    return out


def _load_sanity_check_acknowledged(run_dir: str) -> set[str]:
    """Read scope_declaration.yaml for `sanity_check_acknowledged: [...]`
    entries. Returns the set of check IDs the modeler has explicitly
    documented as out-of-scope."""
    path = os.path.join(run_dir, "scope_declaration.yaml")
    if not os.path.exists(path):
        return set()
    try:
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        return set()
    out: set[str] = set()
    # Top-level `sanity_check_acknowledged: [id1, id2]` form.
    top = doc.get("sanity_check_acknowledged")
    if isinstance(top, list):
        out.update(str(x) for x in top)
    # Per-declaration form: declarations[*].sanity_check_acknowledged
    decls = doc.get("declarations") or []
    if isinstance(decls, list):
        for d in decls:
            if not isinstance(d, dict):
                continue
            ack = d.get("sanity_check_acknowledged")
            if isinstance(ack, list):
                out.update(str(x) for x in ack)
            elif isinstance(ack, str):
                out.add(ack)
    return out


def _self_test() -> int:
    import tempfile as _tempfile

    import yaml as _yaml

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    # SS1-SS5 (Phase 48 β; the check had no unit cases in the validator).
    with _tempfile.TemporaryDirectory() as _d:
        # SS1: before round 3, and with the round unknown, a missing schema is silent.
        ok(gate_findings(_d, round_n=2) == [] and gate_findings(_d, round_n=None) == [],
           "SS1: rounds < 3 and round None are silent")
        # SS2: from round 3 a missing schema is a MEDIUM.
        v = gate_findings(_d, round_n=3)
        ok([x["kind"] for x in v] == ["sanity_schema_missing"] and v[0]["severity"] == "MEDIUM",
           f"SS2: missing schema at r=3 must be one MEDIUM, got {v}")
    with _tempfile.TemporaryDirectory() as _d:
        # SS3: a malformed schema is HIGH sanity_schema_invalid.
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "sanity_schema.yaml"), "w") as f:
            f.write("outcome: [unclosed\n")
        v = gate_findings(_d, round_n=3)
        ok(len(v) == 1 and v[0]["kind"] == "sanity_schema_invalid" and v[0]["severity"] == "HIGH",
           f"SS3: malformed schema must be one HIGH, got {v}")
    with _tempfile.TemporaryDirectory() as _d:
        # SS4: both acknowledgement forms are read; an unreadable file reads as none.
        with open(os.path.join(_d, "scope_declaration.yaml"), "w") as f:
            _yaml.safe_dump({"sanity_check_acknowledged": ["a"],
                             "declarations": [{"sanity_check_acknowledged": ["b"]},
                                              {"sanity_check_acknowledged": "c"}, "x"]}, f)
        ok(_load_sanity_check_acknowledged(_d) == {"a", "b", "c"}, "SS4: both forms read")
        with open(os.path.join(_d, "scope_declaration.yaml"), "w") as f:
            f.write("a: [\n")
        ok(_load_sanity_check_acknowledged(_d) == set(), "SS4b: unreadable file is no acknowledgement")
    # SS5: the validator wires this check under its old name.
    with open(os.path.join(HERE, "validate_critique_yaml.py")) as f:
        _vsrc = f.read()
    ok('_load_sibling("sanity_gate").gate_findings(rd, round_n)' in _vsrc
       and 'check_name="_check_sanity_schema"' in _vsrc,
       "SS5: validator wiring under the old check name")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        print(f"FAILED: {len(failures)} self-test case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    from lib import gate_cli
    return gate_cli.run(argv, __doc__, gate_findings, _self_test, fails=gate_cli.any_high, round_default=None)


if __name__ == "__main__":
    sys.exit(main())
