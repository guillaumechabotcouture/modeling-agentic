"""Rigor artifacts manifest: loader + render helpers.

Single source of truth for the Phase 2 rigor artifact timeline. The
manifest lives at `.claude/orchestration/rigor_artifacts.yaml`; this
module exposes a typed loader and a markdown renderer used by both
`agents/modeler.py` (timeline table) and `scripts/validate_critique_yaml.py`
(artifact path lookup).

Why this exists
---------------
Before this module, every Phase that introduced a new rigor artifact had
to keep ≥4 places in sync: the modeler prompt's timeline table, the
validator's hardcoded path constants, the skill cross-references, and
the CLAUDE.md ledger. Phase 15 α's `identifiability_a_priori` artifact
was the trigger for extracting the manifest — five files had to agree.

Public API
----------
    load_artifacts() -> list[Artifact]         all entries, manifest order
    artifact(id_) -> Artifact                  single entry by id
    artifact_path(id_, run_dir) -> str         absolute path under run_dir
    render_timeline_markdown() -> str          the modeler.py § 4 table
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Iterable

import yaml


_MANIFEST_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..",
                 ".claude", "orchestration", "rigor_artifacts.yaml")
)

_VALID_STAGES = frozenset({"pre_model", "model", "allocation", "write"})
_VALID_TRIGGERS = frozenset({"always", "allocation", "aggregation_ratio_lt_0.1"})


@dataclass(frozen=True)
class Artifact:
    id: str
    path: str
    first_draft_round: int
    finalize_round: int
    stage: str
    skill: str
    triggered_by: str
    scope_declarable: bool
    description: str
    produces: tuple[str, ...] = field(default_factory=tuple)


_cache: list[Artifact] | None = None


def load_artifacts(manifest_path: str | None = None) -> list[Artifact]:
    """Load and validate the manifest. Cached after first call."""
    global _cache
    if manifest_path is None and _cache is not None:
        return _cache
    path = manifest_path or _MANIFEST_PATH
    with open(path) as f:
        doc = yaml.safe_load(f) or {}
    raw = doc.get("artifacts")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path}: top-level `artifacts:` list is missing or empty")
    seen_ids: set[str] = set()
    out: list[Artifact] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: artifacts[{i}] is not a mapping")
        try:
            a = _from_dict(entry)
        except (KeyError, ValueError, TypeError) as e:
            raise ValueError(f"{path}: artifacts[{i}]: {e}") from e
        if a.id in seen_ids:
            raise ValueError(f"{path}: duplicate artifact id {a.id!r}")
        seen_ids.add(a.id)
        out.append(a)
    if manifest_path is None:
        _cache = out
    return out


def _from_dict(d: dict) -> Artifact:
    required = ("id", "path", "first_draft_round", "finalize_round",
                "stage", "skill", "triggered_by", "scope_declarable",
                "description")
    for k in required:
        if k not in d:
            raise KeyError(f"missing required field {k!r}")
    stage = d["stage"]
    if stage not in _VALID_STAGES:
        raise ValueError(f"stage must be one of {sorted(_VALID_STAGES)}, got {stage!r}")
    trig = d["triggered_by"]
    if trig not in _VALID_TRIGGERS:
        raise ValueError(f"triggered_by must be one of {sorted(_VALID_TRIGGERS)}, got {trig!r}")
    fd, fn = int(d["first_draft_round"]), int(d["finalize_round"])
    if fd < 1 or fn < fd:
        raise ValueError(f"rounds invalid: first_draft={fd}, finalize={fn}")
    produces = d.get("produces") or []
    if not isinstance(produces, list) or not all(isinstance(x, str) for x in produces):
        raise ValueError("produces must be a list of strings")
    return Artifact(
        id=str(d["id"]),
        path=str(d["path"]),
        first_draft_round=fd,
        finalize_round=fn,
        stage=stage,
        skill=str(d["skill"]),
        triggered_by=trig,
        scope_declarable=bool(d["scope_declarable"]),
        description=str(d["description"]).strip(),
        produces=tuple(produces),
    )


def artifact(id_: str) -> Artifact:
    """Return the artifact with the given id, or raise KeyError."""
    for a in load_artifacts():
        if a.id == id_:
            return a
    raise KeyError(f"unknown rigor artifact id: {id_!r}")


def artifact_path(id_: str, run_dir: str) -> str:
    """Absolute path to the artifact's manifest file under run_dir."""
    return os.path.join(run_dir, artifact(id_).path)


def produced_path(id_: str, run_dir: str, index: int = 0) -> str:
    """Absolute path to the n-th derived report path declared by the artifact."""
    a = artifact(id_)
    if not a.produces:
        raise KeyError(f"artifact {id_!r} declares no `produces:` paths")
    return os.path.join(run_dir, a.produces[index])


def render_timeline_markdown(artifacts: Iterable[Artifact] | None = None) -> str:
    """Render the modeler.py § 4 timeline table from the manifest.

    The order is the manifest order (so the modeler can curate it
    deliberately by reordering YAML entries, rather than re-deriving
    by round).
    """
    if artifacts is None:
        artifacts = load_artifacts()
    lines = [
        "| Artifact | First draft | Finalize | What \"draft\" means |",
        "|---|---|---|---|",
    ]
    for a in artifacts:
        # Format round windows like the prior hand-written table:
        # singletons "r1" → "**r1 (PRE-MODEL)**" or "r6"; ranges
        # rendered as "r1-2" if first_draft+1 == finalize is unusual,
        # so prefer "r{first}" for the first-draft column and
        # "r{finalize}" for finalize.
        first = _format_round(a, kind="first")
        final = f"r{a.finalize_round}"
        # Collapse description newlines so the markdown table stays
        # one row per artifact.
        desc = " ".join(a.description.split())
        lines.append(f"| `{a.path}` | {first} | {final} | {desc} |")
    return "\n".join(lines)


def _format_round(a: Artifact, kind: str) -> str:
    """Format the first-draft round, with PRE-MODEL highlighting for r1
    pre_model entries (preserves the table's prior emphasis)."""
    if kind == "first":
        if a.stage == "pre_model" and a.first_draft_round == 1:
            return f"**r{a.first_draft_round} (PRE-MODEL)**"
        return f"r{a.first_draft_round}"
    return f"r{a.finalize_round}"


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #


def _self_test() -> int:
    import tempfile

    # T1: real manifest loads cleanly.
    arts = load_artifacts(_MANIFEST_PATH)
    assert len(arts) >= 9, f"expected ≥9 artifacts, got {len(arts)}"
    ids = {a.id for a in arts}
    must = {"identifiability_a_priori", "outcome_fn", "model_comparison",
            "identifiability", "sensitivity_analysis", "allocation_robustness",
            "within_zone_heterogeneity", "sanity_schema", "decision_rule",
            # Phase 17 α + β additions
            "pre_mortem", "coherence_audit",
            # Phase 18 α addition
            "claims_ledger"}
    missing = must - ids
    assert not missing, f"manifest missing required ids: {sorted(missing)}"

    # T2: artifact() and artifact_path() compose correctly.
    a = artifact("outcome_fn")
    assert a.path == "models/outcome_fn.py"
    assert artifact_path("outcome_fn", "/tmp/run") == "/tmp/run/models/outcome_fn.py"
    assert "uncertainty_report.yaml" in a.produces

    # T3: produced_path()
    assert produced_path("outcome_fn", "/tmp/run") == "/tmp/run/uncertainty_report.yaml"
    try:
        produced_path("decision_rule", "/tmp/run")
        assert False, "expected KeyError on artifact with no produces"
    except KeyError:
        pass

    # T4: render_timeline_markdown produces a well-formed table.
    md = render_timeline_markdown()
    assert md.startswith("| Artifact | First draft"), "table header missing"
    assert "models/identifiability_a_priori.yaml" in md
    assert "**r1 (PRE-MODEL)**" in md, "PRE-MODEL emphasis missing for pre_model r1 artifact"
    assert "decision_rule.md" in md
    # All artifacts present.
    for a in arts:
        assert f"`{a.path}`" in md, f"{a.path} missing from rendered table"

    # T5: scope_declarable=False enforced for identifiability_a_priori
    a = artifact("identifiability_a_priori")
    assert not a.scope_declarable, \
        "identifiability_a_priori must be scope_declarable=false (Phase 15 α contract)"

    # T5b: scope_declarable=True enforced for pre_mortem (Phase 17 α
    # contract: pre-mortem concerns are domain heuristics, not
    # arithmetic facts; the modeler may scope-declare a HIGH).
    a = artifact("pre_mortem")
    assert a.scope_declarable, \
        "pre_mortem must be scope_declarable=true (Phase 17 α contract)"

    # T5c: scope_declarable=False enforced for claims_ledger (Phase 18 α
    # contract: writer cannot produce a clean report without a complete
    # ledger; modeler/analyst must produce one or rework).
    a = artifact("claims_ledger")
    assert not a.scope_declarable, \
        "claims_ledger must be scope_declarable=false (Phase 18 α contract)"

    # T6: validation errors on a malformed manifest.
    bad = "artifacts:\n  - id: x\n    path: x.yaml\n"
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(bad)
        bad_path = f.name
    try:
        load_artifacts(bad_path)
        print(f"FAIL T6: expected ValueError on malformed manifest", file=sys.stderr)
        return 1
    except ValueError as e:
        assert "missing required field" in str(e), f"unexpected error: {e}"
    finally:
        os.unlink(bad_path)

    # T7: duplicate ids rejected.
    dup = """artifacts:
  - id: a
    path: a.yaml
    first_draft_round: 1
    finalize_round: 2
    stage: model
    skill: x
    triggered_by: always
    scope_declarable: true
    description: x
  - id: a
    path: a2.yaml
    first_draft_round: 1
    finalize_round: 2
    stage: model
    skill: x
    triggered_by: always
    scope_declarable: true
    description: x
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(dup)
        dup_path = f.name
    try:
        load_artifacts(dup_path)
        print(f"FAIL T7: expected ValueError on duplicate id", file=sys.stderr)
        return 1
    except ValueError as e:
        assert "duplicate artifact id" in str(e), f"unexpected error: {e}"
    finally:
        os.unlink(dup_path)

    # T8: invalid stage / trigger rejected.
    invalid_stage = """artifacts:
  - id: a
    path: a.yaml
    first_draft_round: 1
    finalize_round: 2
    stage: bogus
    skill: x
    triggered_by: always
    scope_declarable: true
    description: x
"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(invalid_stage)
        bs_path = f.name
    try:
        load_artifacts(bs_path)
        print(f"FAIL T8: expected ValueError on invalid stage", file=sys.stderr)
        return 1
    except ValueError as e:
        assert "stage must be one of" in str(e), f"unexpected error: {e}"
    finally:
        os.unlink(bs_path)

    # T9: every artifact's `skill` resolves to an existing SKILL.md.
    repo_root = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
    skills_dir = os.path.join(repo_root, ".claude", "skills")
    if os.path.isdir(skills_dir):
        for a in arts:
            skill_md = os.path.join(skills_dir, a.skill, "SKILL.md")
            assert os.path.exists(skill_md), \
                f"manifest references unknown skill {a.skill!r} for artifact {a.id!r} (no {skill_md})"

    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="run inline test cases")
    parser.add_argument("--render", action="store_true",
                        help="print the rendered timeline markdown table to stdout")
    args = parser.parse_args()
    if args.self_test:
        sys.exit(_self_test())
    if args.render:
        print(render_timeline_markdown())
        sys.exit(0)
    parser.print_help()
    sys.exit(2)
