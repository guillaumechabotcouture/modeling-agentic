#!/usr/bin/env python3
"""Every dynamic import in the repo is either guarded or reviewed (run console review round 3, P1-1).

    python3 scripts/import_audit.py              # list the dynamic import sites and their status
    python3 scripts/import_audit.py --self-test

Trust model: agent-written artifacts in a run dir (specs, derivation records and outputs, links, tmp files) may be
hostile. A module or attribute name that comes from a spec, a benchmark pack or a run artifact is imported only
through toolkit/import_guard.py, whose allowlist policies (engines, oracles, calibrators) refuse anything else
before an import. This audit finds every `importlib.import_module(...)`, `__import__(...)` and
`importlib.util.spec_from_file_location(...)` call in the tracked Python files whose argument is not a string
literal. Each must sit in toolkit/import_guard.py or be listed in REVIEWED below with the reason its name cannot
come from an artifact. A new site, or a reviewed count that no longer matches, fails the self-test, so a new
artifact-fed import cannot be added silently.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = "toolkit/import_guard.py"
SCAN_DIRS = ("agents/", "cloud/", "scripts/", "spike/", "toolkit/", "tools/")
PATTERNS = {
    "import_module": re.compile(r"\bimport_module\(\s*(?![\"'][^\"']*[\"']\s*\))"),
    "__import__": re.compile(r"\b__import__\(\s*(?![\"'][^\"']*[\"']\s*\))"),
    "spec_from_file_location": re.compile(r"\bspec_from_file_location\("),
}
_REPO_FILE = "a module of this repo, named by the code (not by data)"
_RUN_CODE = ("the run's own model code, loaded by path to execute it (the v1 pipeline runs agent-written models "
             "by design; this is not a name lookup)")
# {file: {pattern: (count, reason)}}: the dynamic import sites whose names cannot come from a spec, a pack or an
# artifact. Counts are exact, so a new site in a reviewed file fails too.
REVIEWED = {
    "agents/integrity.py": {"spec_from_file_location": (1, _REPO_FILE)},
    "cloud/run_cluster.py": {"spec_from_file_location": (2, "the operator's cloud config file, and a repo module")},
    "cloud/worker_side.py": {"spec_from_file_location": (1, "a module uploaded by run_cluster.py (repo code)")},
    "scripts/alloc_toolkit_use.py": {"spec_from_file_location": (1, "a string inside its own self-test fixture")},
    "scripts/ask.py": {"__import__": (1, "_sibling(name): sibling script names written in the code")},
    "scripts/benchmark_gate.py": {"spec_from_file_location": (1, _REPO_FILE)},
    "scripts/claim_units.py": {"spec_from_file_location": (1, _REPO_FILE)},
    "scripts/compute.py": {"spec_from_file_location": (3, _RUN_CODE + "; and the repo's worker_side module (twice: "
                                                         "the batch runner and the checkpoint-restore self-test)")},
    "scripts/derived_rebuild.py": {"spec_from_file_location": (2, _REPO_FILE)},
    "scripts/effort_floors_gate.py": {"spec_from_file_location": (1, _REPO_FILE)},
    "scripts/eval_suite.py": {"spec_from_file_location": (2, "the eval suite's own generators and hidden tests")},
    "scripts/gate_fuzz.py": {"import_module": (5, "gate module names from its own table of gates")},
    "scripts/gate_replay.py": {"spec_from_file_location": (1, _REPO_FILE)},
    "scripts/headline_binding.py": {"__import__": (1, "_sibling(name): sibling script names written in the code")},
    "scripts/identifiability.py": {"spec_from_file_location": (1, _RUN_CODE)},
    "scripts/identifiability_gate.py": {"spec_from_file_location": (1, _REPO_FILE)},
    "scripts/lib/expert_priors.py": {"spec_from_file_location": (1, _REPO_FILE)},
    "scripts/lib/positive_control.py": {"spec_from_file_location": (2, _RUN_CODE + "; and its own fixture")},
    "scripts/lib/siblings.py": {"spec_from_file_location": (2, "sibling script names written in the code")},
    "scripts/model_card.py": {"__import__": (1, "_sibling(name): sibling script names written in the code"),
                              "spec_from_file_location": (1, _REPO_FILE)},
    "scripts/pipeline.py": {"spec_from_file_location": (1, _REPO_FILE)},
    "scripts/propagate_uncertainty.py": {"__import__": (1, "_sibling(name): sibling script names written in the code"),
                                         "spec_from_file_location": (1, _RUN_CODE)},
    "scripts/render_claims.py": {"spec_from_file_location": (2, _REPO_FILE)},
    "scripts/replay_coiled.py": {"spec_from_file_location": (3, _REPO_FILE)},
    "scripts/sanity_gate.py": {"spec_from_file_location": (1, _REPO_FILE)},
    "scripts/stall_rule.py": {"spec_from_file_location": (1, _REPO_FILE)},
    "scripts/validate_critique_yaml.py": {"spec_from_file_location": (8, _REPO_FILE)},
    "toolkit/alloc/optimize.py": {"spec_from_file_location": (1, _REPO_FILE)},
    "toolkit/engines/compartmental/selftest_age.py": {
        "__import__": (1, "the literal module toolkit.engines.horizon, in its own self-test")},
    "toolkit/engines/malaria/benchmarks/restamp_proof.py": {
        "import_module": (1, "a module under toolkit.engines.malaria.benchmarks named by the shipped proof file")},
    "toolkit/spec/schema.py": {"import_module": (1, "its self-test's literal (module, class) pairs of shipped engines")},
}


def tracked() -> list[str]:
    out = subprocess.run(["git", "-C", REPO, "ls-files", "*.py"], capture_output=True, text=True, check=True).stdout
    return [p for p in out.splitlines() if p == "main.py" or p.startswith(SCAN_DIRS)]


def sites(paths, read=None) -> dict:
    """{file: {pattern: [line numbers]}} of dynamic import calls (comments skipped; the guard itself skipped)."""
    read = read or (lambda p: open(os.path.join(REPO, p), encoding="utf-8").read())
    out: dict = {}
    for p in paths:
        if p in (GUARD, "scripts/import_audit.py"):       # the guard, and this audit's own patterns
            continue
        try:
            text = read(p)
        except (OSError, UnicodeDecodeError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for name, rx in PATTERNS.items():
                if rx.search(line):
                    out.setdefault(p, {}).setdefault(name, []).append(n)
    return out


def problems(found: dict, reviewed: dict = REVIEWED) -> list[str]:
    bad = []
    for p, pats in sorted(found.items()):
        for name, lines in sorted(pats.items()):
            want = (reviewed.get(p) or {}).get(name)
            if want is None:
                bad.append(f"{p}: {name} at lines {lines} is neither guarded (toolkit/import_guard.py) nor reviewed")
            elif want[0] != len(lines):
                bad.append(f"{p}: {name}: {len(lines)} site(s) at lines {lines}, {want[0]} reviewed")
    for p, pats in sorted(reviewed.items()):
        for name, (n, _) in pats.items():
            if not (found.get(p) or {}).get(name):
                bad.append(f"{p}: {name}: reviewed {n} site(s) but none found (stale entry)")
    return bad


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    fake = {"a.py": "import importlib\nx = importlib.import_module(spec['ref'])\n",
            "b.py": "importlib.import_module('json')\n# importlib.import_module(name)\n",
            "c.py": "m = __import__(name)\n"}
    f = sites(fake, read=lambda p: fake[p])
    ok(set(f) == {"a.py", "c.py"} and f["a.py"] == {"import_module": [2]},
       f"IA1 a call fed by a variable is a site; a literal call or a comment is not ({f})")
    ok(problems(f, {"c.py": {"__import__": (1, "r")}}) == ["a.py: import_module at lines [2] is neither guarded "
                                                           "(toolkit/import_guard.py) nor reviewed"],
       "IA2 an unreviewed site is a problem; a reviewed one is not")
    ok(any("stale" in x for x in problems({}, {"z.py": {"import_module": (1, "r")}})),
       "IA3 a reviewed entry with no site is stale")
    live = problems(sites(tracked()))
    ok(not live, "IA4 every dynamic import in the repo is guarded or reviewed:\n  " + "\n  ".join(live))
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    found = sites(tracked())
    for p, pats in sorted(found.items()):
        for name, lines in sorted(pats.items()):
            r = (REVIEWED.get(p) or {}).get(name)
            print(f"{p}: {name} {lines}: {r[1] if r else 'NOT REVIEWED'}")
    bad = problems(found)
    for b in bad:
        print("PROBLEM", b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
