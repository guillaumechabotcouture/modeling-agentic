"""Typed artifacts of the analysis engine: one envelope, one manifest (design §2).

Every step writes `<run>/analysis/<kind>.yaml`:

    schema: analysis.<kind>/1
    produced_by: harness
    producer: toolkit/analysis/<module>.py
    spec_sha256, spec_signed, engine: {name, version, card_sha256}
    inputs: {<run-relative path>: sha256}
    data: {...}

and `analysis/manifest.json` records each artifact (and each harness-written deliverable)
with its hash, so the contract checks (toolkit/analysis/contract.py) can verify that what
the report renders is what the harness wrote.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os

import numpy as np
import yaml

MANIFEST = os.path.join("analysis", "manifest.json")
PRODUCED_BY = "harness"
PRODUCER_PREFIX = "toolkit/analysis/"
ENVELOPE = ("schema", "produced_by", "producer", "spec_sha256", "spec_signed", "engine", "inputs", "data")
# Required payload fields per kind (the contract checks read these).
KINDS = {
    "engine": ("card", "parameters"),
    "batch": ("evaluation",),
    "emulator": ("accuracy",),
    "calibration": ("method", "units", "diagnostics", "identifiability"),
    "point": ("plan", "averted", "cost_allocated", "mix", "solver"),
    "uncertainty": ("draws", "covers", "excludes", "label", "fixed_plan"),
    "stability": ("method", "folds", "metrics", "verdict", "value_verdict"),
    "sensitivity": ("rows",),
    "decision_tests": ("tests",),
    "figures": ("figures",),
    "claims": ("n_claims", "unresolved"),
}


def sha256(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def plain(x):
    """YAML-safe: numpy scalars and arrays to Python, NaN/inf to None, tuples to lists."""
    if isinstance(x, dict):
        return {str(k): plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [plain(v) for v in x]
    if isinstance(x, np.ndarray):
        return [plain(v) for v in x.tolist()]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating, float)):
        v = float(x)
        return v if math.isfinite(v) else None
    if isinstance(x, np.bool_):
        return bool(x)
    return x


class Store:
    def __init__(self, run_dir: str, spec_sha: str | None, spec_signed: bool, engine_info: dict,
                 settings_sha: str | None = None):
        self.run_dir = os.path.abspath(run_dir)
        self.spec_sha, self.spec_signed, self.engine_info = spec_sha, spec_signed, engine_info
        os.makedirs(os.path.join(self.run_dir, "analysis"), exist_ok=True)
        self.manifest = {"produced_by": PRODUCED_BY, "spec_sha256": spec_sha, "spec_signed": spec_signed,
                         "settings_sha256": settings_sha, "engine": engine_info, "artifacts": {}, "deliverables": {}}

    def rel(self, path: str) -> str:
        return os.path.relpath(os.path.abspath(path), self.run_dir)

    def path(self, rel: str) -> str:
        return os.path.join(self.run_dir, rel)

    def write(self, kind: str, data: dict, producer: str, inputs=(), name: str | None = None) -> str:
        if kind not in KINDS:
            raise ValueError(f"unknown artifact kind {kind}")
        miss = [k for k in KINDS[kind] if k not in data]
        if miss:
            raise ValueError(f"artifact {kind}: payload lacks {miss}")
        rel = os.path.join("analysis", f"{name or kind}.yaml")
        doc = {"schema": f"analysis.{kind}/1", "produced_by": PRODUCED_BY, "producer": producer,
               "spec_sha256": self.spec_sha, "spec_signed": self.spec_signed, "engine": self.engine_info,
               "inputs": {self.rel(p) if os.path.isabs(p) else p: sha256(p if os.path.isabs(p) else self.path(p))
                          for p in inputs},
               "written_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
               "data": plain(data)}
        with open(self.path(rel), "w", encoding="utf-8") as f:
            yaml.safe_dump(doc, f, sort_keys=False, width=120)
        self.manifest["artifacts"][rel] = {"kind": kind, "sha256": sha256(self.path(rel)), "producer": producer}
        self.save()
        return rel

    def deliverable(self, path: str, producer: str, role: str) -> str:
        rel = self.rel(path)
        self.manifest["deliverables"][rel] = {"role": role, "sha256": sha256(path), "producer": producer}
        self.save()
        return rel

    def save(self):
        with open(self.path(MANIFEST), "w", encoding="utf-8") as f:
            json.dump(self.manifest, f, indent=1, sort_keys=True)


def read(run_dir: str, rel: str) -> dict:
    with open(os.path.join(run_dir, rel), encoding="utf-8") as f:
        return yaml.safe_load(f)
