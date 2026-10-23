#!/usr/bin/env python3
"""
Phase 22 γ — build the claims ledger from a spec, so no agent types a number.

Phase 18 α made the writer cite numbers only through `models/claims_ledger.yaml`;
Phase 21 β checked each ledger value against the artifact it cites. E5/E5b
showed what remained: the analyst still typed every value, so ledgers drifted
from their sources. In one fixture 110 of 286 checkable claims contradicted
their source because scenario outputs were re-run after the ledger was written.
Checking catches that after the fact; deriving the values prevents it.

The analyst now writes `models/claims_spec.yaml`: the same entries without
values. This script resolves each claim's `source_artifact` + `source_field`
(the grammar in scripts/lib/provenance.py) and writes the ledger:

  claims_spec.yaml (analyst)  ──►  build_claims_ledger.py  ──►  claims_ledger.yaml
  id, claim_kind, units,            resolves value + CI           spec fields + value,
  source_artifact, source_field,    from the cited artifact       ci_low/ci_high, producer,
  causal_label, confidence, …                                     spec/inputs sha256

Rules:
  - Model-derived claims (any non-external source) must NOT carry value /
    ci_low / ci_high in the spec: the number comes from the artifact. A
    typed value is a spec error.
  - External claims (`source_artifact: external` / `literature`, or a prose
    file such as plan.md) carry their value in the spec, with the citation in
    `source_field`: literature inputs cannot be derived.
  - A claim that cannot be resolved is left out of `claims:` and listed under
    `unresolved:`, so the writer cannot cite it (render_claims leaves its
    reference unresolved). Fix: write the number to an artifact from code,
    then point the claim at it.

render_claims.py and the validator rebuild the ledger from the spec every
time they use it, so the file on disk is a cache that cannot go stale
silently.

Usage:
    python3 scripts/build_claims_ledger.py <run_dir>     # writes the ledger
    python3 scripts/build_claims_ledger.py --self-test
Exit 0 when every claim resolved, 1 otherwise, 2 on a missing/invalid spec.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import datetime, timezone
from typing import Any

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import provenance as P  # noqa: E402

PRODUCER = "scripts/build_claims_ledger.py"
SPEC_REL = os.path.join("models", "claims_spec.yaml")
LEDGER_REL = os.path.join("models", "claims_ledger.yaml")
_VALUE_FIELDS = ("value", "ci_low", "ci_high")


def _sha(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _plain(x: Any) -> Any:
    """YAML-safe scalar (numpy / Decimal-like values become float/str)."""
    if isinstance(x, (bool, int, float, str)) or x is None:
        return x
    n = P._num(x)
    return n if n is not None else str(x)


def build(run_dir: str) -> tuple[dict | None, list[str]]:
    """Return (ledger_doc, spec_errors). ledger_doc is None when the spec is
    missing or unusable; spec_errors lists every structural problem."""
    spec_path = os.path.join(run_dir, SPEC_REL)
    if not os.path.exists(spec_path):
        return None, [f"{SPEC_REL} not found"]
    try:
        with open(spec_path, encoding="utf-8") as f:
            spec = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        return None, [f"{SPEC_REL} could not be parsed: {e}"]
    raw = spec.get("claims") if isinstance(spec, dict) else None
    if not isinstance(raw, list):
        return None, [f"{SPEC_REL}: top-level `claims:` must be a list"]

    errors: list[str] = []
    claims: list[dict] = []
    unresolved: list[dict] = []
    inputs: dict[str, str] = {}
    cache: dict[str, Any] = {}
    seen: set[str] = set()
    for i, c in enumerate(raw):
        if not isinstance(c, dict) or not isinstance(c.get("id"), str):
            errors.append(f"claims[{i}] must be a mapping with a string `id`")
            continue
        cid = c["id"]
        if cid in seen:
            errors.append(f"duplicate claim id {cid!r}")
            continue
        seen.add(cid)
        src = c.get("source_artifact")
        out = dict(c)
        if P.is_external(src):
            if c.get("value") is None:
                errors.append(f"[{cid}] external claim needs `value` (and a citation "
                              f"in source_field)")
                continue
            claims.append(out)
            continue
        typed = [k for k in _VALUE_FIELDS if k in c]
        if typed:
            errors.append(f"[{cid}] model-derived claim carries {typed}; remove them: "
                          f"the value is read from `{src}`")
            continue
        try:
            point, ci = P.resolve_claim(c, run_dir, cache)
        except P.ClaimUnresolved as e:
            unresolved.append({"id": cid, "reason": str(e)})
            continue
        out["value"] = _plain(point)
        if ci is not None and None not in ci:
            out["ci_low"], out["ci_high"] = _plain(ci[0]), _plain(ci[1])
        path = P._resolve_artifact_path(run_dir, str(src))
        if path:
            inputs[os.path.relpath(path, run_dir)] = _sha(path) or ""
        claims.append(out)

    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "producer": PRODUCER,
        "spec_sha256": _sha(spec_path),
        "inputs_sha256": dict(sorted(inputs.items())),
        "n_claims": len(claims),
        "claims": claims,
        "unresolved": unresolved,
    }
    for k in ("conventions",):
        if isinstance(spec, dict) and k in spec:
            doc[k] = spec[k]
    return doc, errors


def _key(c: dict) -> tuple:
    return tuple(_plain(c.get(k)) for k in ("id", "value", "ci_low", "ci_high"))


def compare(on_disk: Any, rebuilt: dict) -> list[str]:
    """Differences between a ledger file and a fresh build: stale or edited."""
    if not isinstance(on_disk, dict):
        return ["ledger file is not a mapping"]
    probs = []
    if on_disk.get("producer") != PRODUCER:
        probs.append(f"producer is {on_disk.get('producer')!r}, not {PRODUCER!r}")
    disk = {c.get("id"): c for c in on_disk.get("claims") or [] if isinstance(c, dict)}
    new = {c["id"]: c for c in rebuilt["claims"]}
    for cid in sorted(set(disk) | set(new)):
        if cid not in new:
            probs.append(f"[{cid}] is in the file but not in a fresh build")
        elif cid not in disk:
            probs.append(f"[{cid}] is missing from the file")
        elif not all(P.values_match(a, b, str(new[cid].get("claim_kind") or ""))
                     if a is not None and b is not None else a == b
                     for a, b in zip(_key(disk[cid])[1:], _key(new[cid])[1:])):
            probs.append(f"[{cid}] file has {_key(disk[cid])[1:]}, source now gives "
                         f"{_key(new[cid])[1:]}")
    return probs


def write(run_dir: str) -> tuple[dict | None, list[str]]:
    doc, errors = build(run_dir)
    if doc is not None:
        with open(os.path.join(run_dir, LEDGER_REL), "w", encoding="utf-8") as f:
            yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)
    return doc, errors


def _self_test() -> int:
    import tempfile
    fails: list[str] = []

    def ok(c, m):
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        w = lambda rel, obj: yaml.safe_dump(obj, open(os.path.join(d, rel), "w"))
        w("uncertainty_report.yaml", {"scalar_outputs": {"total": {"mean": 1200.5, "ci_low": 900,
                                                                    "ci_high": 1500}}})
        w("models/sens.yaml", {"verdict": "SENSITIVE", "cost_3.60": {"n": 248}})
        open(os.path.join(d, "models", "alloc.csv"), "w").write("unit,cost\nA,100\nB,250\n")
        spec = {"claims": [
            {"id": "total", "claim_kind": "scalar", "units": "dalys",
             "source_artifact": "uncertainty_report.yaml", "source_field": "scalar_outputs.total"},
            {"id": "verdict", "claim_kind": "label", "source_artifact": "models/sens.yaml",
             "source_field": "verdict"},
            {"id": "n_changed", "claim_kind": "count", "source_artifact": "models/sens.yaml",
             "source_field": "cost_3.60.n"},
            {"id": "cost_sum", "claim_kind": "currency", "units": "usd",
             "source_artifact": "models/alloc.csv", "source_field": "cost.sum()"},
            {"id": "burden", "claim_kind": "scalar", "value": 12.8e6, "units": "dalys",
             "source_artifact": "external", "source_field": "GBD 2021 (PMID 0)"},
        ]}
        w("models/claims_spec.yaml", spec)
        doc, errs = write(d)
        got = {c["id"]: c for c in doc["claims"]}
        ok(errs == [] and doc["unresolved"] == [], f"B1 clean spec builds: {errs} {doc['unresolved']}")
        ok(got["total"]["value"] == 1200.5 and got["total"]["ci_low"] == 900, f"B2 value+CI from source: {got['total']}")
        ok(got["verdict"]["value"] == "SENSITIVE" and got["n_changed"]["value"] == 248
           and got["cost_sum"]["value"] == 350.0, f"B3 label/dotted key/CSV agg: {got}")
        ok(got["burden"]["value"] == 12.8e6, "B4 external value kept from spec")
        ok(doc["producer"] == PRODUCER and "uncertainty_report.yaml" in doc["inputs_sha256"], "B5 stamps")

        # B6: a typed value on a model-derived claim is a spec error
        spec["claims"][0]["value"] = 1300
        w("models/claims_spec.yaml", spec)
        _, errs = build(d)
        ok(any("[total] model-derived claim carries ['value']" in e for e in errs), f"B6 typed value rejected: {errs}")
        del spec["claims"][0]["value"]
        # B7: free-text field → unresolved, excluded from claims
        spec["claims"].append({"id": "prose", "claim_kind": "scalar", "source_artifact": "models/sens.yaml",
                               "source_field": "zone-level weighted average"})
        w("models/claims_spec.yaml", spec)
        doc2, errs = build(d)
        ok([u["id"] for u in doc2["unresolved"]] == ["prose"] and "prose" not in {c["id"] for c in doc2["claims"]},
           f"B7 unresolved excluded: {doc2['unresolved']}")
        # B8: source re-run after the ledger was written → compare() reports stale
        w("uncertainty_report.yaml", {"scalar_outputs": {"total": {"mean": 1400, "ci_low": 1000,
                                                                    "ci_high": 1700}}})
        on_disk = yaml.safe_load(open(os.path.join(d, LEDGER_REL)))
        fresh, _ = build(d)
        probs = compare(on_disk, fresh)
        ok(any(p.startswith("[total] file has") for p in probs), f"B8 stale ledger detected: {probs}")
        # B9: a hand-edited ledger (right stamp, wrong number) is detected the same way
        _, _ = write(d)
        on_disk = yaml.safe_load(open(os.path.join(d, LEDGER_REL)))
        on_disk["claims"][2]["value"] = 340
        ok(any(p.startswith("[n_changed]") for p in compare(on_disk, build(d)[0])), "B9 edited ledger detected")
        ok(compare(yaml.safe_load(open(os.path.join(d, LEDGER_REL))), build(d)[0]) == [], "B10 fresh ledger is current")
        # B11: missing spec
        os.remove(os.path.join(d, "models", "claims_spec.yaml"))
        ok(build(d) == (None, [f"{SPEC_REL} not found"]), "B11 missing spec")

    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Build models/claims_ledger.yaml from models/claims_spec.yaml")
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir required")
    doc, errors = write(a.run_dir)
    for e in errors:
        print(f"SPEC ERROR: {e}")
    if doc is None:
        return 2
    for u in doc["unresolved"]:
        print(f"UNRESOLVED [{u['id']}]: {u['reason']}")
    print(f"wrote {LEDGER_REL}: {doc['n_claims']} claims, {len(doc['unresolved'])} unresolved, "
          f"{len(errors)} spec errors")
    return 0 if not errors and not doc["unresolved"] else (2 if errors else 1)


if __name__ == "__main__":
    sys.exit(main())
