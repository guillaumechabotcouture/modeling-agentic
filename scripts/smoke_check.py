#!/usr/bin/env python3
"""Check that a smoke run exercised the changed code: every expected marker is in the run.

On 2026-11-05 a re-run meant to test a v2 fix ran the v1 pipeline, and nothing noticed: the run
finished, so it looked like a pass. A PR's real smoke therefore names markers only the changed
code produces (a field it records, a file only it writes, a state its stage machine reaches), and
this checker fails when any is absent. Disease-agnostic: markers are paths and fields.

    python3 scripts/smoke_check.py <run_dir> --expect SPEC [--expect SPEC ...] [--pipeline v2] [--commit SHA]

SPEC (paths are relative to <run_dir>; a path may be a glob, and every match must satisfy it):
    PATH               the file exists
    PATH:KEY           the JSON/YAML field exists and is not null (KEY is dotted; a list index is a number)
    PATH:KEY=VALUE     the field equals VALUE (compared as text; true/false/null as JSON spells them)
    PATH:KEY^=PREFIX   the field's text starts with PREFIX (a commit id)
    PATH~REGEX         the file's text matches REGEX
--pipeline P adds metadata.json:pipeline=P; --commit SHA adds metadata.json:commit^=SHA.

Exit 0 when every marker is present, 1 when any is missing (each one listed), 2 on a usage error.
A file that cannot be read or parsed is a missing marker, never a pass.

    python3 scripts/smoke_check.py --self-test
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

_MISSING = object()


def parse_spec(spec: str) -> dict:
    """{path, kind: exists|field|equals|prefix|regex, key, value}."""
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("empty marker")
    if "~" in spec:
        path, rx = spec.split("~", 1)
        re.compile(rx)
        return {"path": path, "kind": "regex", "key": None, "value": rx}
    if ":" in spec:
        path, expr = spec.split(":", 1)
        if "^=" in expr:
            key, val = expr.split("^=", 1)
            return {"path": path, "kind": "prefix", "key": key, "value": val}
        if "=" in expr:
            key, val = expr.split("=", 1)
            return {"path": path, "kind": "equals", "key": key, "value": val}
        return {"path": path, "kind": "field", "key": expr, "value": None}
    return {"path": spec, "kind": "exists", "key": None, "value": None}


def _load(path: str):
    text = open(path, encoding="utf-8", errors="replace").read()
    if path.endswith((".yaml", ".yml")):
        import yaml
        return yaml.safe_load(text)
    if path.endswith(".jsonl"):
        return [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    return json.loads(text)


def _get(obj, key: str):
    for part in [p for p in key.split(".") if p != ""]:
        if isinstance(obj, list) and re.fullmatch(r"-?\d+", part):
            i = int(part)
            obj = obj[i] if -len(obj) <= i < len(obj) else _MISSING
        elif isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            return _MISSING
        if obj is _MISSING:
            return _MISSING
    return obj


def _text(v) -> str:
    return json.dumps(v) if isinstance(v, bool) or v is None else str(v)


def check_marker(run_dir: str, m: dict) -> str | None:
    """None when the marker is present, else why it is not."""
    matches = sorted(glob.glob(os.path.join(run_dir, m["path"])))
    if not matches:
        return f"{m['path']}: no such file"
    for p in matches:
        rel = os.path.relpath(p, run_dir)
        if m["kind"] == "exists":
            continue
        try:
            if m["kind"] == "regex":
                if not re.search(m["value"], open(p, encoding="utf-8", errors="replace").read(), re.M):
                    return f"{rel}: no match for /{m['value']}/"
                continue
            v = _get(_load(p), m["key"])
        except Exception as e:  # noqa: BLE001 -- an unreadable marker file is a missing marker
            return f"{rel}: cannot be read ({type(e).__name__}: {str(e)[:80]})"
        if v is _MISSING or v is None:
            return f"{rel}: no field {m['key']}"
        if m["kind"] == "equals" and _text(v) != m["value"]:
            return f"{rel}: {m['key']} is {_text(v)!r}, expected {m['value']!r}"
        if m["kind"] == "prefix" and not _text(v).startswith(m["value"]):
            return f"{rel}: {m['key']} is {_text(v)!r}, expected it to start with {m['value']!r}"
    return None


def check(run_dir: str, specs: list[str]) -> list[str]:
    """Every missing marker, as a line; [] when all are present. No marker at all is an error."""
    if not os.path.isdir(run_dir):
        return [f"{run_dir}: not a directory"]
    if not specs:
        return ["no markers given: a smoke check must name at least one"]
    out = []
    for s in specs:
        try:
            why = check_marker(run_dir, parse_spec(s))
        except (ValueError, re.error) as e:
            why = f"bad marker {s!r}: {e}"
        if why:
            out.append(f"MISSING {s}  ({why})")
    return out


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    with tempfile.TemporaryDirectory() as td:
        json.dump({"pipeline": "v2", "commit": "abc1234def", "flag": True, "rounds": [1, 2],
                   "nested": {"stage": "WRITE"}, "none": None}, open(os.path.join(td, "metadata.json"), "w"))
        open(os.path.join(td, "run_state.json"), "w").write('{"stage": "DONE", "history": [{"to": "FIX_LOOP"}]}')
        open(os.path.join(td, "trace.jsonl"), "w").write('{"e": "start"}\n{"e": "stage", "to": "ANALYZE"}\n')
        os.makedirs(os.path.join(td, "figures"))
        open(os.path.join(td, "figures", "a.png"), "wb").write(b"x")
        open(os.path.join(td, "spec.yaml"), "w").write("engine:\n  name: renewal\n")
        # Today's evidence: a v1 run where a v2 run was meant: the marker fails.
        v1 = os.path.join(td, "v1run")
        os.makedirs(v1)
        json.dump({"question": "Q", "status": "completed"}, open(os.path.join(v1, "metadata.json"), "w"))
        ok(check(v1, ["metadata.json:pipeline=v2"]) != [], "SC1 a run that records no pipeline fails pipeline=v2")
        ok(check(v1, ["run_state.json"]) != [], "SC2 a v1 run has no run_state.json: the marker fails")
        good = ["metadata.json:pipeline=v2", "metadata.json:commit^=abc1234", "metadata.json:flag=true",
                "metadata.json:rounds.1=2", "metadata.json:nested.stage", "run_state.json:history.0.to=FIX_LOOP",
                "trace.jsonl~\"to\": \"ANALYZE\"", "figures/*.png", "spec.yaml:engine.name=renewal"]
        ok(check(td, good) == [], f"SC3 every marker kind passes when present: {check(td, good)}")
        for bad in ("metadata.json:pipeline=v1", "metadata.json:commit^=fff", "metadata.json:none",
                    "metadata.json:rounds.5", "figures/*.svg", "trace.jsonl~FIX_LOOP", "nofile.json:x"):
            ok(len(check(td, [bad])) == 1, f"SC4 a missing marker fails: {bad}")
        open(os.path.join(td, "broken.json"), "w").write("{not json")
        ok("cannot be read" in " ".join(check(td, ["broken.json:x"])), "SC5 an unparsable file fails closed")
        ok(check(td, []) != [] and check(os.path.join(td, "nope"), ["x"]) != [],
           "SC6 no markers, or no run directory, is a failure")
        ok(check(td, ["metadata.json~[unclosed"]) != [], "SC7 a bad regex is a failure, not a crash")
        rc = main([td, "--pipeline", "v2", "--commit", "abc1234", "--expect", "run_state.json:stage=DONE"])
        ok(rc == 0, f"SC8 the CLI passes a run that holds every marker ({rc})")
        ok(main([v1, "--pipeline", "v2"]) == 1, "SC9 the CLI fails the 2026-11-05 run (no pipeline recorded)")
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
    ap.add_argument("run_dir")
    ap.add_argument("--expect", action="append", default=[], help="a marker (see above); repeatable")
    ap.add_argument("--pipeline", choices=("v1", "v2"), help="adds metadata.json:pipeline=<P>")
    ap.add_argument("--commit", help="adds metadata.json:commit^=<SHA>")
    a = ap.parse_args(argv)
    specs = list(a.expect)
    if a.pipeline:
        specs.insert(0, f"metadata.json:pipeline={a.pipeline}")
    if a.commit:
        specs.insert(1 if a.pipeline else 0, f"metadata.json:commit^={a.commit}")
    missing = check(a.run_dir, specs)
    for s in specs:
        if not any(m.startswith(f"MISSING {s}  ") for m in missing):
            print(f"present  {s}")
    for m in missing:
        print(m)
    print(f"smoke check: {len(specs) - len(missing) if specs else 0}/{len(specs)} markers present in {a.run_dir}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
