#!/usr/bin/env python3
"""Gate replay: re-run the STAGE 7 validator on saved runs and diff the findings.

    python3 scripts/gate_replay.py run [<run_dir> ...] [--runs-file list.txt] --out findings.json \
        [--repo <repo_root>] [--jobs 4] [--timeout 900]
    python3 scripts/gate_replay.py diff a.json b.json
    python3 scripts/gate_replay.py --self-test

Phase 46 α. A refactor of the gate must not change what the gate finds. This
script records, per run, what `scripts/validate_critique_yaml.py` produces in
two modes, each on its own fresh copy of the run (some checks write files; the
source run directory is never touched):

  cli    the validator CLI exactly as main.py's final gate calls it
         (`--spec-compliance --parameter-registry --rigor-artifacts --json`,
         `--post-write` when report.md exists), plus `--no-history`. The
         exit code, the decision's scalar fields and every finding list are
         recorded. A run the CLI refuses (exit 2, e.g. no citations.md) is
         recorded with its last ERROR line: whatever the validator produces.
  rigor  `_check_rigor_artifacts(run, round_n, post_write)` in-process, so the
         rigor checks are compared even on runs the CLI refuses before them.

Each finding keeps kind, severity, stage, target_stage, scope_declarable, id,
reviewer, first_seen_round, its position in its list, and the claim text with
the copy's path, the repo path and ISO timestamps replaced by placeholders.
Lists are sorted, so the output is canonical; the position field keeps any
order change visible. `diff` exits 0 when two outputs are identical, 1 when not.

The repo whose validator runs is `--repo` (default: this script's repo), so a
baseline can be recorded from a checkout of origin/main.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DEFAULT = os.path.dirname(HERE)
DEFAULT_MAX_ROUNDS = 8
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_FINDING_FIELDS = ("kind", "severity", "stage", "target_stage", "scope_declarable",
                   "id", "reviewer", "first_seen_round", "category", "accept_blocker")


def detect_current_round(run_dir: str) -> tuple[int, str]:
    """main.py's detect_current_round (highest critique `round:`, then
    pipeline_state.yaml), with round 1 when neither says, so the rigor
    checks still replay on runs main.py would not gate."""
    import yaml
    rounds = []
    for path in glob.glob(os.path.join(run_dir, "critique_*.yaml")):
        try:
            with open(path) as f:
                doc = yaml.safe_load(f)
            r = doc.get("round") if isinstance(doc, dict) else None
            if isinstance(r, int):
                rounds.append(r)
        except Exception:  # noqa: BLE001
            continue
    if rounds:
        return max(rounds), "critiques"
    try:
        with open(os.path.join(run_dir, "pipeline_state.yaml")) as f:
            state = yaml.safe_load(f) or {}
        r = state.get("current_round") if isinstance(state, dict) else None
        if isinstance(r, int):
            return r, "pipeline_state"
    except Exception:  # noqa: BLE001
        pass
    return 1, "default"


def detect_max_rounds(run_dir: str) -> int:
    try:
        with open(os.path.join(run_dir, "metadata.json")) as f:
            v = json.load(f).get("max_rounds")
        if isinstance(v, int) and v > 0:
            return v
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_MAX_ROUNDS


def copy_run(src: str, dst: str) -> None:
    """Copy a run directory. On macOS `cp -c` makes copy-on-write clones, so
    a 1.4 GB run copies in a moment and writes never reach the source."""
    if sys.platform == "darwin":
        proc = subprocess.run(["cp", "-cRp", src, dst], capture_output=True, text=True)
        if proc.returncode == 0:
            return
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, symlinks=True)


class Normaliser:
    """Replace the given absolute paths (and, with `rel_from`, their paths
    relative to it: the registry check reports code paths relative to the
    validator's cwd, the repo) by <PATH>, and ISO timestamps by <TS>."""

    def __init__(self, *paths: str, rel_from: str | None = None):
        subs = set()
        for p in paths:
            if p:
                for q in {p, os.path.realpath(p)}:
                    subs.add(q.rstrip("/"))
                    if rel_from:
                        for base in {rel_from, os.path.realpath(rel_from)}:
                            r = os.path.relpath(q, base)
                            if r.startswith(".."):
                                subs.add(r)
        self.subs = sorted(subs, key=len, reverse=True)

    def text(self, s) -> str:
        s = str(s)
        for i, p in enumerate(self.subs):
            s = s.replace(p, f"<P{i}>")
        s = re.sub(r"<P\d+>", "<PATH>", s)
        return _TS_RE.sub("<TS>", s)

    def value(self, v):
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, dict):
            return {str(k): self.value(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [self.value(x) for x in v]
        return v


def canon_findings(items: list, norm: Normaliser) -> list[dict]:
    out = []
    for pos, it in enumerate(items):
        if not isinstance(it, dict):
            out.append({"pos": pos, "raw": norm.value(it)})
            continue
        rec = {"pos": pos}
        for k in _FINDING_FIELDS:
            if k in it:
                rec[k] = norm.value(it[k])
        claim = norm.text(it.get("claim", ""))
        rec["claim"] = claim
        rec["claim_sha"] = hashlib.sha256(claim.encode()).hexdigest()[:16]
        extra = {k: v for k, v in it.items() if k not in _FINDING_FIELDS and k != "claim"}
        if extra:
            rec["other"] = norm.value(extra)
        out.append(rec)
    return sorted(out, key=lambda r: json.dumps(r, sort_keys=True, default=str))


def canon_decision(decision: dict, norm: Normaliser) -> dict:
    out = {"scalars": {}, "sections": {}}
    for k in sorted(decision):
        v = decision[k]
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            out["sections"][k] = canon_findings(v, norm)
        elif isinstance(v, list) and not v:
            out["sections"][k] = []
        else:
            out["scalars"][k] = norm.value(v)
    return out


def _run_cli(repo: str, copy: str, max_rounds: int, current_round: int,
             timeout: int) -> dict:
    validator = os.path.join(repo, "scripts", "validate_critique_yaml.py")
    cmd = [sys.executable, validator, copy, "--max-rounds", str(max_rounds),
           "--current-round", str(current_round), "--spec-compliance",
           "--parameter-registry", "--rigor-artifacts", "--json", "--no-history"]
    if os.path.exists(os.path.join(copy, "report.md")):
        cmd.append("--post-write")
    try:
        proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "error": f"timeout after {timeout}s"}
    rec = {"exit_code": proc.returncode, "post_write": "--post-write" in cmd}
    out = proc.stdout.strip()
    decision = None
    if "{" in out:
        try:
            decision = json.loads(out[out.index("{"):])
        except json.JSONDecodeError:
            decision = None
    if isinstance(decision, dict):
        rec["decision"] = decision
    else:
        errs = [ln for ln in proc.stderr.splitlines() if "ERROR" in ln or "Error" in ln]
        rec["error"] = (errs or proc.stderr.strip().splitlines() or ["no JSON on stdout"])[-1]
    return rec


def _run_rigor(repo: str, copy: str, current_round: int, timeout: int) -> dict:
    post_write = os.path.exists(os.path.join(copy, "report.md"))
    cmd = [sys.executable, os.path.abspath(__file__), "_rigor", repo, copy,
           str(current_round), "1" if post_write else "0"]
    try:
        proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"timeout after {timeout}s"}
    out = proc.stdout.strip()
    marker = "\n@@GATE_REPLAY@@\n"
    if marker.strip() in out:
        try:
            return {"violations": json.loads(out.split(marker.strip(), 1)[1])}
        except json.JSONDecodeError:
            pass
    tail = (proc.stderr.strip().splitlines() or ["no output"])[-1]
    return {"error": f"exit {proc.returncode}: {tail}"}


def _rigor_child(repo: str, copy: str, round_n: int, post_write: bool) -> int:
    import importlib.util
    path = os.path.join(repo, "scripts", "validate_critique_yaml.py")
    sys.path.insert(0, os.path.dirname(path))
    spec = importlib.util.spec_from_file_location("validate_critique_yaml", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["validate_critique_yaml"] = mod
    spec.loader.exec_module(mod)
    v = mod._check_rigor_artifacts(copy, round_n=round_n, post_write=post_write)
    sys.stdout.flush()
    print("\n@@GATE_REPLAY@@\n" + json.dumps(v, default=str))
    return 0


def replay_one(src: str, repo: str, timeout: int) -> dict:
    src = os.path.abspath(src)
    current_round, round_source = detect_current_round(src)
    max_rounds = detect_max_rounds(src)
    rec = {"run": os.path.basename(src.rstrip("/")), "current_round": current_round,
           "round_source": round_source, "max_rounds": max_rounds}
    tmp = tempfile.mkdtemp(prefix="gate_replay_")
    try:
        for mode in ("cli", "rigor"):
            copy = os.path.join(tmp, mode, "run")
            os.makedirs(os.path.dirname(copy))
            copy_run(src, copy)
            norm = Normaliser(copy, tmp, src, repo, rel_from=repo)
            if mode == "cli":
                r = _run_cli(repo, copy, max_rounds, current_round, timeout)
                if "decision" in r:
                    r["decision"] = canon_decision(r.pop("decision"), norm)
                if "error" in r:
                    r["error"] = norm.text(r["error"])
            else:
                r = _run_rigor(repo, copy, current_round, timeout)
                if "violations" in r:
                    r["violations"] = canon_findings(r["violations"], norm)
                if "error" in r:
                    r["error"] = norm.text(r["error"])
            rec[mode] = r
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rec


def replay(runs: list[str], repo: str, jobs: int = 4, timeout: int = 900,
           progress: bool = True) -> dict:
    keys = [os.path.abspath(r) for r in runs]

    def work(r):
        res = replay_one(r, repo, timeout)
        if progress:
            n_cli = sum(len(s) for s in res["cli"].get("decision", {}).get("sections", {}).values())
            n_rig = len(res["rigor"].get("violations", []))
            print(f"[replay] {res['run']}: cli exit={res['cli'].get('exit_code')} "
                  f"findings={n_cli} rigor={n_rig}", file=sys.stderr, flush=True)
        return res

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        results = list(ex.map(work, keys))
    by_run = {}
    for k, res in zip(keys, results):
        name = res["run"]
        if name in by_run:   # two corpora with the same run name
            name = f"{name}@{hashlib.sha256(k.encode()).hexdigest()[:8]}"
        by_run[name] = res
    return {"schema": "gate_replay/1", "runs": dict(sorted(by_run.items()))}


def count_findings(doc: dict) -> int:
    n = 0
    for res in doc.get("runs", {}).values():
        n += sum(len(s) for s in res.get("cli", {}).get("decision", {}).get("sections", {}).values())
        n += len(res.get("rigor", {}).get("violations", []))
    return n


def diff(a: dict, b: dict) -> list[str]:
    """Human-readable differences between two replay outputs ([] = identical)."""
    out = []
    ra, rb = a.get("runs", {}), b.get("runs", {})
    for name in sorted(set(ra) | set(rb)):
        if name not in ra or name not in rb:
            out.append(f"{name}: only in {'A' if name in ra else 'B'}")
            continue
        out += _diff_value(ra[name], rb[name], name)
    return out


def _diff_value(x, y, where: str) -> list[str]:
    if isinstance(x, dict) and isinstance(y, dict):
        out = []
        for k in sorted(set(x) | set(y), key=str):
            if k not in x or k not in y:
                out.append(f"{where}.{k}: only in {'A' if k in x else 'B'}")
            else:
                out += _diff_value(x[k], y[k], f"{where}.{k}")
        return out
    if isinstance(x, list) and isinstance(y, list):
        if x == y:
            return []
        sx = {json.dumps(i, sort_keys=True, default=str) for i in x}
        sy = {json.dumps(i, sort_keys=True, default=str) for i in y}
        out = [f"{where}: -{i[:300]}" for i in sorted(sx - sy)]
        out += [f"{where}: +{i[:300]}" for i in sorted(sy - sx)]
        return out or [f"{where}: list order or multiplicity differs"]
    return [] if x == y else [f"{where}: {json.dumps(x, default=str)[:200]} != {json.dumps(y, default=str)[:200]}"]


def _run_self_test() -> int:
    failures = []

    def ok(cond, label):
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    print("gate_replay self-test")
    norm = Normaliser("/tmp/x/run", "/repo")
    ok(norm.text("see /tmp/x/run/models/a.py at 2026-10-28T20:03:27.384167")
       == "see <PATH>/models/a.py at <TS>", "N1: paths and timestamps normalised")
    rn = Normaliser("/tmp/x/run", rel_from="/a/b/repo")
    ok(rn.text("../../../tmp/x/run/models/o.py:9 tags") == "<PATH>/models/o.py:9 tags",
       "N2: a path relative to the repo is normalised (the registry check reports these)")
    f = canon_findings([{"kind": "b", "severity": "HIGH", "claim": "x /repo/y"},
                        {"kind": "a", "severity": "MEDIUM", "claim": "z", "extra": 1}], norm)
    ok([r["kind"] for r in f] == ["b", "a"] and f[0]["pos"] == 0 and f[0]["claim"] == "x <PATH>/y"
       and f[1]["other"] == {"extra": 1}, "C1: findings keep fields and position, sorted canonically")
    d = canon_decision({"action": "PATCH", "unresolved_high": [{"kind": "k", "claim": "c"}],
                        "spec_violations": [], "blocker_attempts": {}}, norm)
    ok(d["scalars"]["action"] == "PATCH" and len(d["sections"]["unresolved_high"]) == 1
       and d["sections"]["spec_violations"] == [], "C2: decision split into scalars and sections")
    A = {"runs": {"r": {"cli": {"exit_code": 1, "decision": d}}}}
    B = json.loads(json.dumps(A))
    ok(diff(A, B) == [], "D1: identical outputs diff empty")
    B["runs"]["r"]["cli"]["decision"]["sections"]["unresolved_high"][0]["severity"] = "HIGH"
    ok(len(diff(A, B)) == 2, "D2: a changed finding shows as -/+")
    B2 = json.loads(json.dumps(A))
    B2["runs"]["s"] = {}
    ok(diff(A, B2) == ["s: only in B"], "D3: a missing run is reported")

    # End to end on a tiny run: source untouched, replay deterministic.
    with tempfile.TemporaryDirectory() as td:
        run = os.path.join(td, "2026-01-01_000000_tiny")
        os.makedirs(run)
        with open(os.path.join(run, "metadata.json"), "w") as fh:
            json.dump({"question": "Build an SIR model.", "max_rounds": 3}, fh)
        with open(os.path.join(run, "pipeline_state.yaml"), "w") as fh:
            fh.write("current_round: 2\n")
        before = sorted(os.listdir(run))
        ok(detect_current_round(run) == (2, "pipeline_state") and detect_max_rounds(run) == 3,
           "R1: round and max rounds read like main.py")
        r1 = replay([run], REPO_DEFAULT, jobs=1, timeout=600, progress=False)
        r2 = replay([run], REPO_DEFAULT, jobs=1, timeout=600, progress=False)
        res = r1["runs"]["2026-01-01_000000_tiny"]
        ok(sorted(os.listdir(run)) == before, "R2: source run directory untouched")
        cli = res["cli"]
        ok(cli.get("exit_code") == 3 and "<PATH>/critique_domain.yaml" in json.dumps(
            cli.get("decision", {}).get("scalars", {}).get("schema_errors")),
           f"R3: CLI refusal (schema errors, exit 3) recorded as produced ({cli})")
        ok(isinstance(res["rigor"].get("violations"), list) and bool(res["rigor"]["violations"]),
           f"R4: rigor findings recorded (fail-closed findings included): {res['rigor'].get('error')}")
        ok(diff(r1, r2) == [], "R5: replay deterministic")
        ok(td not in json.dumps(r1), "R6: no temp or source path survives normalisation")

    if failures:
        print(f"FAILED: {len(failures)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "_rigor":
        _, _, repo, copy, rnd, pw = sys.argv
        return _rigor_child(repo, copy, int(rnd), pw == "1")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--self-test", action="store_true")
    sub = p.add_subparsers(dest="cmd")
    pr = sub.add_parser("run")
    pr.add_argument("runs", nargs="*")
    pr.add_argument("--runs-file", help="file listing run directories, one per line")
    pr.add_argument("--out", required=True)
    pr.add_argument("--repo", default=REPO_DEFAULT)
    pr.add_argument("--jobs", type=int, default=4)
    pr.add_argument("--timeout", type=int, default=900)
    pd = sub.add_parser("diff")
    pd.add_argument("a")
    pd.add_argument("b")
    args = p.parse_args()
    if args.self_test:
        return _run_self_test()
    if args.cmd == "run":
        listed = list(args.runs)
        if args.runs_file:
            with open(args.runs_file) as fh:
                listed += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
        runs = [r for r in listed if os.path.isdir(r)]
        skipped = sorted(set(listed) - set(runs))
        for s in skipped:
            print(f"[replay] skipped (not a directory): {s}", file=sys.stderr)
        doc = replay(runs, os.path.abspath(args.repo), args.jobs, args.timeout)
        with open(args.out, "w") as fh:
            json.dump(doc, fh, indent=1, sort_keys=True)
        print(f"[replay] {len(doc['runs'])} runs, {count_findings(doc)} findings -> {args.out}",
              file=sys.stderr)
        return 0
    if args.cmd == "diff":
        with open(args.a) as fa, open(args.b) as fb:
            a, b = json.load(fa), json.load(fb)
        d = diff(a, b)
        print(f"runs: A={len(a.get('runs', {}))} B={len(b.get('runs', {}))}; "
              f"findings: A={count_findings(a)} B={count_findings(b)}; differences: {len(d)}")
        for line in d[:200]:
            print("  " + line)
        return 0 if not d else 1
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
