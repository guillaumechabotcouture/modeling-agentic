#!/usr/bin/env python3
"""Phase 42 ε: an allocation run uses the allocation toolkit, or records why not.

    python3 scripts/alloc_toolkit_use.py <run_dir> [--json]
    python3 scripts/alloc_toolkit_use.py --self-test

E7 attempts 7 and 8 wrote their own `models/costs.py` and `models/optimize.py`,
the most finding-dense code of attempt 7, because nothing pointed the modeler
to `toolkit/alloc` (costs, packages, optimize). The `allocation-toolkit` skill
says how to use it. This check, run by the gate when the run produces an
allocation, reads which toolkit modules the run's code uses:

- a Python file under models/ (sub-directories included) that imports
  `toolkit.alloc.<module>` or `from toolkit.alloc import <module>`, or names
  the file `toolkit/alloc/<module>.py` (loading it by path);
- a `run` command in models/pipeline.yaml that names `toolkit/alloc/<module>.py`.

A module the run does not use is recorded with the reason in
`models/toolkit_use.yaml`, and so is each extension the run writes in
`models/ext/`:

    not_used:                    # toolkit module -> why this run does not use it
      optimize: "<what the run needs that the toolkit's optimizer does not do>"
    extensions:                  # models/ext/ file -> what the toolkit lacks
      models/ext/cost_items.py: "<what it adds>"

Findings are advisory (MEDIUM, scope-declarable): `alloc_toolkit_not_used`
for modules neither used nor explained, `alloc_toolkit_extension_unexplained`
for models/ext/ files without a reason, `alloc_toolkit_use_invalid` for a
record that does not parse. No allocation, no findings.
"""
from __future__ import annotations

import argparse
import ast
import glob
import json
import os
import re
import sys
import tempfile

import yaml

MODULES = ("costs", "packages", "optimize")
RECORD = os.path.join("models", "toolkit_use.yaml")
EXT_DIR = os.path.join("models", "ext")
# The same globs as the gate's allocation detection (validate_critique_yaml._ALLOCATION_GLOBS).
ALLOCATION_GLOBS = ("*allocation*.csv", "*budget*.csv", "*optimization*.csv")
_PATH_REF = re.compile(r"toolkit/alloc/(costs|packages|optimize)\.py")


def allocation_present(run_dir: str) -> bool:
    for pat in ALLOCATION_GLOBS:
        for sub in ("", "data", "models"):
            if glob.glob(os.path.join(run_dir, sub, pat)):
                return True
    return os.path.exists(os.path.join(run_dir, "decision_rule.md"))


def _py_files(run_dir: str) -> list[str]:
    out = []
    for dp, dirs, files in os.walk(os.path.join(run_dir, "models")):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".ma_compute_bundle")]
        out += [os.path.join(dp, f) for f in files if f.endswith(".py")]
    return sorted(out)


def modules_used(source: str) -> set[str]:
    """Toolkit modules a Python source imports or loads by path."""
    used: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return used
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                if parts[:2] == ["toolkit", "alloc"] and len(parts) > 2 and parts[2] in MODULES:
                    used.add(parts[2])
        elif isinstance(node, ast.ImportFrom) and not node.level:
            parts = (node.module or "").split(".")
            if parts[:2] != ["toolkit", "alloc"]:
                continue
            if len(parts) > 2:
                if parts[2] in MODULES:
                    used.add(parts[2])
            else:
                used |= {a.name for a in node.names if a.name in MODULES}
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            used |= set(_PATH_REF.findall(node.value))
    return used


def scan(run_dir: str) -> dict:
    """{"used": {module: [files]}, "record": dict | None, "record_error": str | None, "extensions": [paths]}."""
    used: dict[str, list[str]] = {}
    for p in _py_files(run_dir):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                mods = modules_used(f.read())
        except OSError:
            continue
        for m in mods:
            used.setdefault(m, []).append(os.path.relpath(p, run_dir))
    try:
        with open(os.path.join(run_dir, "models", "pipeline.yaml"), encoding="utf-8") as f:
            pl = yaml.safe_load(f) or {}
        for st in (pl.get("steps") or []) if isinstance(pl, dict) else []:
            if isinstance(st, dict):
                for m in _PATH_REF.findall(str(st.get("run") or "")):
                    used.setdefault(m, []).append(f"models/pipeline.yaml step {st.get('id')}")
    except (OSError, yaml.YAMLError, AttributeError):
        pass       # the pipeline gate reports an unreadable pipeline
    record, err = None, None
    rp = os.path.join(run_dir, RECORD)
    if os.path.exists(rp):
        try:
            with open(rp, encoding="utf-8") as f:
                record = yaml.safe_load(f) or {}
            if not isinstance(record, dict):
                raise ValueError("not a mapping")
            for k in ("not_used", "extensions"):
                if record.get(k) is not None and not isinstance(record.get(k), dict):
                    raise ValueError(f"`{k}` must be a mapping")
        except (OSError, ValueError, yaml.YAMLError) as e:
            record, err = None, f"{type(e).__name__}: {str(e)[:200]}"
    ext = sorted(os.path.relpath(p, run_dir).replace(os.sep, "/") for p in _py_files(run_dir)
                 if os.path.relpath(p, run_dir).startswith(EXT_DIR + os.sep)
                 and os.path.basename(p) != "__init__.py")
    return {"used": used, "record": record, "record_error": err, "extensions": ext}


def _reason(d, key) -> bool:
    return isinstance(d, dict) and isinstance(d.get(key), str) and bool(d[key].strip())


def gate_findings(run_dir: str, allocation: bool | None = None) -> list[dict]:
    if allocation is None:
        allocation = allocation_present(run_dir)
    if not allocation:
        return []
    s = scan(run_dir)
    base = {"severity": "MEDIUM", "stage": "MODEL", "target_stage": "MODEL", "scope_declarable": True}
    out = []
    if s["record_error"]:
        out.append({**base, "kind": "alloc_toolkit_use_invalid",
                    "claim": f"{RECORD} does not parse ({s['record_error']}); see the `allocation-toolkit` skill."})
    rec = s["record"] or {}
    missing = [m for m in MODULES if m not in s["used"] and not _reason(rec.get("not_used"), m)]
    if missing:
        out.append({**base, "kind": "alloc_toolkit_not_used",
                    "claim": (f"This run produces an allocation, and its code does not use toolkit/alloc "
                              f"{', '.join(missing)} or record why in {RECORD} (`not_used: {{<module>: <reason>}}`). "
                              "Declare costs and packages as a spec and use the toolkit's costs, packages and "
                              "optimize modules; see the `allocation-toolkit` skill.")})
    unexplained = [p for p in s["extensions"] if not _reason(rec.get("extensions"), p)]
    if unexplained:
        out.append({**base, "kind": "alloc_toolkit_extension_unexplained",
                    "claim": (f"{len(unexplained)} file(s) in {EXT_DIR}/ have no reason in {RECORD} "
                              f"(`extensions: {{<path>: <what the toolkit lacks>}}`): {', '.join(unexplained[:6])}.")})
    return out


# --------------------------------------------------------------------------- self-test

def _self_test() -> int:
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    def w(d, rel, text):
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def kinds(d, **kw):
        return sorted(f["kind"] for f in gate_findings(d, **kw))

    ok(modules_used("import toolkit.alloc.costs as c\nfrom toolkit.alloc import packages, optimize as o\n")
       == set(MODULES), "AT1 plain and from-imports of the toolkit are recognised")
    ok(modules_used("from toolkit.alloc.optimize import benchmark\n") == {"optimize"}
       and modules_used("spec_from_file_location('c', REPO + '/toolkit/alloc/costs.py')\n") == {"costs"},
       "AT2 a from-import of a function and a load by path count")
    ok(modules_used("# from toolkit.alloc import costs\nimport costs\nfrom .toolkit.alloc import optimize\n") == set()
       and modules_used("def (:\n") == set(),
       "AT3 a comment, a run's own costs.py, a relative import or a syntax error are not toolkit use")
    with tempfile.TemporaryDirectory() as d:
        w(d, "models/model.py", "x = 1\n")
        ok(kinds(d) == [], "AT4 no allocation: nothing to check")
        w(d, "models/allocation.csv", "unit,package\n")
        ok(kinds(d) == ["alloc_toolkit_not_used"] and kinds(d, allocation=False) == [],
           "AT5 an allocation with hand-written costs and optimizer: MEDIUM; the gate's allocation flag is honoured")
        f = gate_findings(d)[0]
        ok(f["severity"] == "MEDIUM" and f["scope_declarable"] and "toolkit/alloc costs, packages, optimize or" in f["claim"],
           f"AT6 advisory, declarable, and names the modules: {f}")
        w(d, "models/ext/alloc_run.py", "from toolkit.alloc import costs, packages\n")
        w(d, "models/pipeline.yaml", "steps:\n  - id: allocate\n    run: python3 ../../toolkit/alloc/optimize.py "
                                     "spec.yaml --benefit b.csv --budget 1\n    inputs: []\n    outputs: [x]\n")
        ok(kinds(d) == ["alloc_toolkit_extension_unexplained"],
           f"AT7 toolkit used from models/ext/ and the pipeline; the extension needs a reason: {kinds(d)}")
        w(d, RECORD, "extensions:\n  models/ext/alloc_run.py: per-outcome costs from the model's own output table\n")
        ok(kinds(d) == [], "AT8 an explained extension clears it")
        os.remove(os.path.join(d, "models/pipeline.yaml"))
        ok("does not use toolkit/alloc optimize or record" in gate_findings(d)[0]["claim"],
           "AT9 only the unused module is named")
        w(d, RECORD, "not_used:\n  optimize: '  '\n")
        ok("alloc_toolkit_not_used" in kinds(d), "AT10 a blank reason is no reason")
        w(d, RECORD, "not_used:\n  optimize: the objective couples units through transmission; annealing\n"
                     "extensions:\n  models/ext/alloc_run.py: x\n")
        ok(kinds(d) == [], "AT11 a recorded reason clears the module")
        w(d, RECORD, "not_used: [optimize]\n")
        ok("alloc_toolkit_use_invalid" in kinds(d) and "alloc_toolkit_not_used" in kinds(d),
           "AT12 a malformed record fails closed (MEDIUM) and explains nothing")
    if fails:
        print("SELF-TEST FAILURES:")
        for m in fails:
            print("  -", m)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir required")
    fs = gate_findings(a.run_dir)
    if a.json:
        print(json.dumps({"scan": {k: v for k, v in scan(a.run_dir).items()}, "findings": fs}, indent=1, default=str))
    else:
        for f in fs:
            print(f"{f['severity']} {f['kind']}: {f['claim']}")
        print(f"{len(fs)} finding(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
