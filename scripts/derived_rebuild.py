#!/usr/bin/env python3
"""Phase 39 β: the harness regenerates stale figures itself.

    python3 scripts/derived_rebuild.py <run_dir> [--json]
    python3 scripts/derived_rebuild.py --self-test

E7 attempt 7: every model change left figures older than the outputs they
show. `figure_staleness_detected` was raised at the gates of rounds 2, 3 and 5,
and agents regenerated figures by hand, incompletely, round after round. Yet
each figure's provenance sidecar (`<png>.provenance.json`, written by
`figure_validator.validate_figure`) already records the script that drew it
and the hashes of its inputs.

`rebuild(run_dir)` finds the figures whose inputs changed, re-runs each of
their generator scripts once (from the run directory, in the order the
figures were first written), and checks staleness again. Only scripts inside
the run directory are run. The harness calls it after each modeler and
analyst stage, so the analyst and the critics read current figures, and
before the final gate. Figures stay a modeling tool every round; nobody
spends a round regenerating them.

It never marks files as written by the harness: a figure script that rewrites
a protected artifact is still reported by the integrity record. Scripts run
without cloud credentials (as agents do without --cloud), each in its own
process group, within a total time budget below the CLI's hook deadline.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import figure_validator as fv  # noqa: E402
import pipeline  # noqa: E402  (S1: figures a pipeline step declares are rebuilt by the pipeline run)

REPORT = os.path.join("figures", "rebuild_report.json")
SCRIPT_TIMEOUT_S = 300
HOOK_BUDGET_S = 420        # the CLI abandons a hook after 600 s; stop well before it


def _cloud_prefixes() -> tuple[str, ...]:
    """The credential prefixes agents' environments are stripped of (agents/enforcement.py)."""
    import importlib.util
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agents", "enforcement.py")
    try:
        spec = importlib.util.spec_from_file_location("_ma_enforcement", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return tuple(mod.CLOUD_ENV_PREFIXES)
    except Exception:  # noqa: BLE001 — fail closed: strip the known families
        return ("AZ_", "AZURE_", "OPENAI_", "AWS_", "GOOGLE_")


def script_env(environ=None) -> dict:
    env = dict(os.environ if environ is None else environ)
    pre = _cloud_prefixes()
    return {k: v for k, v in env.items() if not k.startswith(pre)}


def _sidecars(run_dir: str) -> list[tuple[str, dict]]:
    out = []
    for sp in sorted(glob.glob(os.path.join(run_dir, "figures", "**", "*.png.provenance.json"), recursive=True)):
        try:
            out.append((sp[: -len(".provenance.json")], json.load(open(sp))))
        except (OSError, ValueError):
            continue
    return out


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:
        return False


def stale_scripts(run_dir: str) -> tuple[list[str], dict[str, list[str]], list[dict]]:
    """(scripts to run in order, figures per script, figures that cannot be rebuilt)."""
    order, figs, skipped = [], {}, []
    try:
        owned = pipeline.declared_outputs(run_dir)
    except Exception:  # noqa: BLE001 — fail closed: a pipeline we cannot read owns every figure
        owned = ["*"] if os.path.exists(os.path.join(run_dir, pipeline.SPEC)) else []
    for png, sc in sorted(_sidecars(run_dir), key=lambda t: str(t[1].get("written_at_utc") or "")):
        if fv.check_staleness(png)["status"] != "stale":
            continue
        script = sc.get("generator_script")
        rel = os.path.relpath(png, run_dir)
        if any(pipeline.path_match(rel.replace(os.sep, "/"), p) for p in owned):
            # Its writer is a declared step: only the harness pipeline run may rewrite it.
            skipped.append({"figure": rel, "why": f"a declared output of {pipeline.SPEC}: rebuilt by the pipeline run"})
            continue
        if not script:
            skipped.append({"figure": rel, "why": "no generator script recorded"})
            continue
        if not _inside(script, run_dir):
            # Sidecars written on another machine (or a moved run) record absolute
            # paths elsewhere: re-anchor them on this run directory by their
            # run-relative tail when that file exists.
            tail = script.split(os.path.basename(os.path.normpath(run_dir)) + os.sep, 1)
            cand = os.path.join(run_dir, tail[1]) if len(tail) == 2 else None
            if cand and os.path.isfile(cand) and _inside(cand, run_dir):
                script = cand
            else:
                skipped.append({"figure": rel, "why": f"generator script outside the run directory: {script}"})
                continue
        if not os.path.isfile(script):
            skipped.append({"figure": rel, "why": f"generator script missing: {script}"})
            continue
        if script not in figs:
            order.append(script)
            figs[script] = []
        figs[script].append(rel)
    return order, figs, skipped


def _run(cmd, cwd, timeout_s, env):
    """Run in its own process group; on timeout the whole group is killed, so
    nothing keeps writing after the rebuild returns."""
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         text=True, env=env, start_new_session=True)
    try:
        _out, err = p.communicate(timeout=timeout_s)
        return p.returncode, (err or "")[-600:]
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        return None, f"timed out after {timeout_s:.0f}s (process group killed)"


def rebuild(run_dir: str, timeout_s: float = SCRIPT_TIMEOUT_S, python: str | None = None,
            budget_s: float | None = None) -> dict:
    run_dir = os.path.realpath(run_dir)
    t0 = time.time()
    order, figs, skipped = stale_scripts(run_dir)
    env = script_env()
    runs = []
    for script in order:
        s0 = time.time()
        left = None if budget_s is None else budget_s - (s0 - t0)
        if left is not None and left < 5:
            runs.append({"script": os.path.relpath(script, run_dir), "figures": figs[script], "returncode": None,
                         "seconds": 0.0, "stderr_tail": "not started: rebuild time budget used"})
            continue
        try:
            rc, err = _run([python or sys.executable, script], run_dir,
                           timeout_s if left is None else min(timeout_s, left), env)
        except OSError as e:
            rc, err = None, f"{type(e).__name__}: {e}"
        runs.append({"script": os.path.relpath(script, run_dir), "figures": figs[script], "returncode": rc,
                     "seconds": round(time.time() - s0, 1), **({"stderr_tail": err} if rc != 0 else {})})
    still = [os.path.relpath(png, run_dir) for png, _ in _sidecars(run_dir) if fv.check_staleness(png)["status"] == "stale"]
    report = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "seconds": round(time.time() - t0, 1), "runs": runs,
              "skipped": skipped, "still_stale": still,
              "rebuilt": sorted({f for r in runs if r["returncode"] == 0 for f in r["figures"]} - set(still))}
    try:
        os.makedirs(os.path.join(run_dir, "figures"), exist_ok=True)
        with open(os.path.join(run_dir, REPORT), "w") as f:
            json.dump(report, f, indent=1)
    except OSError:
        pass
    return report


def summary_line(rep: dict) -> str:
    bad = [r for r in rep["runs"] if r["returncode"] != 0]
    return (f"derived rebuild: {len(rep['rebuilt'])} figures regenerated by {len(rep['runs']) - len(bad)} scripts"
            + (f"; {len(bad)} scripts failed ({', '.join(r['script'] for r in bad)})" if bad else "")
            + (f"; still stale: {', '.join(rep['still_stale'])}" if rep["still_stale"] else "")
            + f" [{rep['seconds']}s]")


def _self_test() -> int:
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    here = os.path.dirname(os.path.abspath(__file__))
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        os.makedirs(os.path.join(d, "models")); os.makedirs(os.path.join(d, "figures"))
        data = os.path.join(d, "models", "out.csv")
        open(data, "w").write("x,y\n1,2\n")
        # A figure script that writes a "PNG" and its sidecar the way real scripts do.
        fig = (f"import sys, os\nsys.path.insert(0, {here!r})\nimport figure_validator as fv\n"
               "d = os.getcwd()\n"
               "open(os.path.join(d, 'figures', 'a.png'), 'w').write(open(os.path.join(d, 'models', 'out.csv')).read())\n"
               "fv.validate_figure(os.path.join(d, 'figures', 'a.png'), [os.path.join(d, 'models', 'out.csv')])\n")
        open(os.path.join(d, "models", "figs.py"), "w").write(fig)
        bad = "raise SystemExit('boom')\n"
        open(os.path.join(d, "models", "bad.py"), "w").write(bad)
        r = subprocess.run([sys.executable, os.path.join(d, "models", "figs.py")], cwd=d, capture_output=True, text=True)
        ok(r.returncode == 0 and os.path.exists(os.path.join(d, "figures", "a.png.provenance.json")),
           f"DR0 fixture: the figure script writes a sidecar: {r.stderr[-300:]}")
        rep = rebuild(d)
        ok(rep["runs"] == [] and rep["still_stale"] == [], f"DR1 nothing stale: nothing runs: {rep}")
        open(data, "w").write("x,y\n1,3\n")
        rep = rebuild(d)
        ok([x["script"] for x in rep["runs"]] == [os.path.join("models", "figs.py")] and rep["rebuilt"] == ["figures/a.png"]
           and rep["still_stale"] == [] and open(os.path.join(d, "figures", "a.png")).read().endswith("1,3\n"),
           f"DR2 a changed input re-runs its figure's script and the figure is current: {rep}")
        ok(os.path.exists(os.path.join(d, REPORT)) and "1 figures regenerated" in summary_line(rep),
           f"DR3 the rebuild is reported: {summary_line(rep)}")
        # A sidecar naming a failing script: reported, figure still stale.
        sc = json.load(open(os.path.join(d, "figures", "a.png.provenance.json")))
        json.dump({**sc, "generator_script": os.path.join(d, "models", "bad.py")},
                  open(os.path.join(d, "figures", "a.png.provenance.json"), "w"))
        open(data, "w").write("x,y\n1,4\n")
        rep = rebuild(d)
        ok(rep["runs"][0]["returncode"] == 1 and rep["still_stale"] == ["figures/a.png"] and "failed" in summary_line(rep),
           f"DR4 a failing script is reported and its figure stays stale: {rep}")
        # Outside the run directory: never run.
        json.dump({**sc, "generator_script": "/tmp/elsewhere/evil.py"},
                  open(os.path.join(d, "figures", "a.png.provenance.json"), "w"))
        rep = rebuild(d)
        ok(rep["runs"] == [] and rep["skipped"] and "outside the run directory" in rep["skipped"][0]["why"],
           f"DR5 a script outside the run directory is not run: {rep}")
        # A symlink inside the run directory pointing outside it: not run.
        outside = tempfile.mkdtemp()
        open(os.path.join(outside, "evil.py"), "w").write("open('PWNED', 'w').write('x')\n")
        os.symlink(os.path.join(outside, "evil.py"), os.path.join(d, "models", "link.py"))
        json.dump({**sc, "generator_script": os.path.join(d, "models", "link.py")},
                  open(os.path.join(d, "figures", "a.png.provenance.json"), "w"))
        rep = rebuild(d)
        ok(rep["runs"] == [] and not os.path.exists(os.path.join(d, "PWNED")),
           f"DR5b (Codex) a symlink to a script outside the run directory is not run: {rep}")
        other_ln = os.path.join("/tmp/ab/R1/runs", os.path.basename(d), "models", "link.py")
        json.dump({**sc, "generator_script": other_ln}, open(os.path.join(d, "figures", "a.png.provenance.json"), "w"))
        rep = rebuild(d)
        ok(rep["runs"] == [], f"DR5c (Codex) re-anchoring checks containment too: {rep}")
        # Environment: no cloud credentials; a slow script's process group is killed.
        ok("AZURE_CLIENT_SECRET" not in script_env({"AZURE_CLIENT_SECRET": "s", "HOME": "/h"})
           and script_env({"HOME": "/h"})["HOME"] == "/h", "DR7 (Codex) scripts run without cloud credentials")
        open(os.path.join(d, "models", "slow.py"), "w").write(
            "import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', "
            "'import time, os; time.sleep(3); open(\"CHILD_RAN\", \"w\").write(\"x\")'])\ntime.sleep(60)\n")
        json.dump({**sc, "generator_script": os.path.join(d, "models", "slow.py")},
                  open(os.path.join(d, "figures", "a.png.provenance.json"), "w"))
        t1 = time.time()
        rep = rebuild(d, timeout_s=1.0)
        time.sleep(3.5)
        ok(rep["runs"][0]["returncode"] is None and time.time() - t1 < 10 and not os.path.exists(os.path.join(d, "CHILD_RAN")),
           f"DR8 (Codex) a timed-out script's whole process group is killed: {rep}")
        rep = rebuild(d, timeout_s=1.0, budget_s=0)
        ok("budget" in rep["runs"][0].get("stderr_tail", ""), f"DR9 nothing starts once the time budget is used: {rep}")
        # Recorded on another machine: re-anchored on this run directory.
        other = os.path.join("/tmp/ab/R1/runs", os.path.basename(d), "models", "figs.py")
        json.dump({**sc, "generator_script": other}, open(os.path.join(d, "figures", "a.png.provenance.json"), "w"))
        rep = rebuild(d)
        ok([x["script"] for x in rep["runs"]] == [os.path.join("models", "figs.py")] and rep["runs"][0]["returncode"] == 0,
           f"DR6 a sidecar from another machine is re-anchored by its run-relative path: {rep}")
        # S1: a figure a pipeline step declares is the pipeline run's to rebuild.
        json.dump(sc, open(os.path.join(d, "figures", "a.png.provenance.json"), "w"))
        open(data, "w").write("x,y\n1,5\n")
        open(os.path.join(d, "models", "pipeline.yaml"), "w").write(
            "steps:\n  - id: figs\n    run: python3 models/figs.py\n    inputs: [models/out.csv]\n"
            "    outputs: ['figures/*.png']\n")
        rep = rebuild(d)
        ok(rep["runs"] == [] and any("pipeline" in s["why"] for s in rep["skipped"]),
           f"DR10 a figure declared in models/pipeline.yaml is left to the pipeline run: {rep}")
    if fails:
        print("SELF-TEST FAILURES:")
        for f in fails:
            print("  -", f)
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
    rep = rebuild(a.run_dir)
    print(json.dumps(rep, indent=1) if a.json else summary_line(rep))
    return 0 if not rep["still_stale"] else 1


if __name__ == "__main__":
    sys.exit(main())
