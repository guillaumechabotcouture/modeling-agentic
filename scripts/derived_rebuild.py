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

Phase 47 α (A9): a generator that redraws its PNG without calling
`validate_figure` again leaves the sidecar's old source hashes, so the figure
stayed stale after every rebuild (attempt 8's data-stage eda.py: rounds 1-5,
`figure_staleness_detected` HIGH in rounds 2 and 4). After a script exits 0,
the harness re-records the source hashes of each of its stale figures whose
PNG the run rewrote (mtime or content changed against a snapshot taken
before the run) and whose sidecar the script left unchanged, keeping the
sidecar's other fields and marking it `provenance_by: derived_rebuild`. A
figure stays stale when its PNG was not rewritten, its script failed, a
recorded source is missing, or a recorded annotation no longer appears in
the script.

It marks as written by the harness only the sidecars it re-records itself: a
figure script that rewrites a protected artifact is still reported by the
integrity record. Scripts run
without cloud credentials (as agents do without --cloud), each in its own
process group, within a total time budget below the CLI's hook deadline.
"""
from __future__ import annotations

import argparse
import datetime
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
from lib.safe_io import inside_dir  # noqa: E402


def _load_pipeline():
    """scripts/pipeline.py by path under a private name. A plain `import pipeline` can
    return a run's own models/pipeline.py already in sys.modules (E7 attempt 7; the
    collision validate_critique_yaml._load_sibling was fixed for)."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline.py")
    spec = importlib.util.spec_from_file_location("_ma_derived_pipeline", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pipeline = _load_pipeline()   # S1: figures a pipeline step declares are rebuilt by the pipeline run

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


def _stale_or_unreadable(png: str) -> bool:
    """check_staleness, with a sidecar it cannot read counted as stale (fail closed)."""
    try:
        return fv.check_staleness(png)["status"] == "stale"
    except Exception:  # noqa: BLE001 — malformed provenance: stale
        return True


def stale_scripts(run_dir: str) -> tuple[list[str], dict[str, list[str]], list[dict]]:
    """(scripts to run in order, figures per script, figures that cannot be rebuilt)."""
    order, figs, skipped = [], {}, []
    try:
        owned = pipeline.declared_outputs(run_dir)
    except Exception:  # noqa: BLE001 — fail closed: a pipeline we cannot read owns every figure
        owned = ["*"] if os.path.exists(os.path.join(run_dir, pipeline.SPEC)) else []
    for png, sc in sorted(_sidecars(run_dir), key=lambda t: str(t[1].get("written_at_utc") or "")):
        if not _stale_or_unreadable(png):
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


PROVENANCE_BY = "derived_rebuild"


def _file_state(path: str) -> tuple | None:
    """(mtime_ns, size, sha256) of a file, or None when it is missing or unreadable."""
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size, fv._hash_file(path))
    except OSError:
        return None


def _recorded_sources(sp: str) -> dict | None:
    """The sidecar's recorded source paths -> current sha256, or None when unreadable."""
    try:
        with open(sp) as f:
            sc = json.load(f)
        srcs = sc.get("source_hashes") if isinstance(sc, dict) else None
        if not isinstance(srcs, dict):
            return None
        return {str(p): fv._hash_file(str(p)) for p in srcs}
    except (OSError, ValueError, TypeError):
        return None


def _snapshot(run_dir: str, rels: list[str]) -> dict[str, dict]:
    """Each figure's PNG, sidecar and recorded-source state, taken before its script runs."""
    out = {}
    for rel in rels:
        png = os.path.join(run_dir, rel)
        sp = fv._provenance_path(png)
        out[rel] = {"png": _file_state(png), "sc": _file_state(sp), "src": _recorded_sources(sp)}
    return out


def _candidate(run_dir: str, rel: str, before: dict, script: str) -> tuple[dict | None, str | None]:
    """A9, right after the figure's own script exited 0: (candidate, None) when the
    sidecar may be re-recorded once every script has finished, else (None, why)."""
    png = os.path.join(run_dir, rel)
    sp = fv._provenance_path(png)
    png_after, sc_after = _file_state(png), _file_state(sp)
    if png_after is None:
        return None, "PNG missing after its script ran"
    pb = before["png"]
    if pb is not None and png_after[0] == pb[0] and png_after[2] == pb[2]:
        return None, "its script did not rewrite the PNG"
    if sc_after is None or sc_after != before["sc"]:
        return None, "its script rewrote or removed the sidecar itself"
    try:
        with open(sp) as f:
            sc = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"sidecar unreadable: {type(e).__name__}"
    srcs = sc.get("source_hashes") if isinstance(sc, dict) else None
    if not isinstance(srcs, dict) or not srcs:
        return None, "sidecar records no sources"
    missing = [p for p in srcs if not os.path.isfile(str(p))]
    if missing:
        return None, f"recorded source missing: {missing[0]}"
    now = {str(p): fv._hash_file(str(p)) for p in srcs}
    if before["src"] is None or now != before["src"]:
        # It may have drawn from the old content and then rewritten its input.
        return None, "a recorded source changed while its script ran"
    ann = sc.get("expected_annotations")
    if ann is not None and (not isinstance(ann, list) or not all(isinstance(a, str) for a in ann)):
        return None, f"expected_annotations is not a list of strings: {ann!r}"[:200]
    if ann:
        # validate_figure's own annotation check, applied to the script that redrew it.
        try:
            with open(script) as f:
                text = fv._strip_validate_figure_calls(f.read())
        except OSError as e:
            return None, f"generator script unreadable: {type(e).__name__}"
        gone = [a for a in ann if a not in text]
        if gone:
            return None, f"recorded annotation no longer in its script: {gone[0]!r}"
    return {"png": png_after, "sc": sc_after, "src": now, "doc": sc}, None


def _commit(run_dir: str, rel: str, cand: dict, integrity=None) -> str | None:
    """A9, after every script has finished: write the re-recorded sidecar if nothing
    changed since the figure's own run. Returns None when written, else why not."""
    png = os.path.join(run_dir, rel)
    sp = fv._provenance_path(png)
    tmp = sp + ".tmp"
    if os.path.islink(png) or os.path.islink(sp):
        return "PNG or sidecar is a symlink"
    if not all(inside_dir(run_dir, p) for p in (png, sp, tmp)):
        return "PNG, sidecar or temp file resolves outside the run directory"
    if _file_state(png) != cand["png"]:
        return "the PNG changed after its own script's run (a later script rewrote it)"
    if _file_state(sp) != cand["sc"]:
        return "the sidecar changed after its script's run"
    if {p: fv._hash_file(p) for p in cand["src"]} != cand["src"]:
        return "a recorded source changed after its script's run"
    sc = dict(cand["doc"])
    sc["source_hashes"] = dict(cand["src"])
    sc["provenance_by"] = PROVENANCE_BY
    sc["provenance_rerecorded_at_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "w") as f:
            json.dump(sc, f, indent=2, sort_keys=True)
        if not inside_dir(run_dir, sp) or os.path.islink(sp):
            os.unlink(tmp)
            return "sidecar resolves outside the run directory"
        os.replace(tmp, sp)
    except OSError as e:
        return f"sidecar not writable: {type(e).__name__}"
    if integrity is not None:
        # The harness wrote this sidecar; the script left it unchanged (checked above).
        integrity.harness_wrote(os.path.relpath(sp, run_dir).replace(os.sep, "/"))
    return None


def rebuild(run_dir: str, timeout_s: float = SCRIPT_TIMEOUT_S, python: str | None = None,
            budget_s: float | None = None, integrity=None) -> dict:
    run_dir = os.path.realpath(run_dir)
    t0 = time.time()
    order, figs, skipped = stale_scripts(run_dir)
    env = script_env()
    runs, rerecorded, not_rerecorded, candidates = [], [], [], {}
    for script in order:
        s0 = time.time()
        left = None if budget_s is None else budget_s - (s0 - t0)
        if left is not None and left < 5:
            runs.append({"script": os.path.relpath(script, run_dir), "figures": figs[script], "returncode": None,
                         "seconds": 0.0, "stderr_tail": "not started: rebuild time budget used"})
            continue
        before = _snapshot(run_dir, figs[script])
        try:
            rc, err = _run([python or sys.executable, script], run_dir,
                           timeout_s if left is None else min(timeout_s, left), env)
        except OSError as e:
            rc, err = None, f"{type(e).__name__}: {e}"
        runs.append({"script": os.path.relpath(script, run_dir), "figures": figs[script], "returncode": rc,
                     "seconds": round(time.time() - s0, 1), **({"stderr_tail": err} if rc != 0 else {})})
        if rc != 0:
            continue
        for rel in figs[script]:
            # One figure's problem never stops the others or the remaining scripts.
            try:
                if fv.check_staleness(os.path.join(run_dir, rel))["status"] != "stale":
                    continue
                cand, why = _candidate(run_dir, rel, before[rel], script)
            except Exception as e:  # noqa: BLE001
                cand, why = None, f"provenance unreadable: {type(e).__name__}: {e}"[:200]
            if cand is not None:
                candidates[rel] = cand
            else:
                not_rerecorded.append({"figure": rel, "why": why})
    # Deferred until every script has finished, so a later script that rewrites a
    # figure (and then fails) cannot leave it marked fresh.
    for rel, cand in candidates.items():
        try:
            why = _commit(run_dir, rel, cand, integrity)
        except Exception as e:  # noqa: BLE001
            why = f"sidecar not re-recorded: {type(e).__name__}: {e}"[:200]
        if why is None:
            rerecorded.append(rel)
        else:
            not_rerecorded.append({"figure": rel, "why": why})
    still = [os.path.relpath(png, run_dir) for png, _ in _sidecars(run_dir) if _stale_or_unreadable(png)]
    report = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "seconds": round(time.time() - t0, 1), "runs": runs,
              "skipped": skipped, "still_stale": still,
              "provenance_rerecorded": rerecorded, "provenance_not_rerecorded": not_rerecorded,
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
            + (f"; provenance re-recorded by the harness for {len(rep['provenance_rerecorded'])}"
               if rep.get("provenance_rerecorded") else "")
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
        # (pipeline review #5) An unparsable pipeline.yaml owns every figure; it does not hand
        # them back to this rebuild.
        open(os.path.join(d, "models", "pipeline.yaml"), "w").write("steps: [\n")
        rep = rebuild(d)
        ok(rep["runs"] == [] and any("pipeline" in s["why"] for s in rep["skipped"]),
           f"DR11 (pipeline review #5) an unparsable models/pipeline.yaml keeps its figures from this rebuild: {rep}")
    # Phase 47 α (A9): generators that redraw a PNG without calling validate_figure again
    # (attempt 8's eda.py). The harness re-records the sidecar only when the run rewrote
    # the PNG, the script exited 0 and left the sidecar alone.
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        os.makedirs(os.path.join(d, "models")); os.makedirs(os.path.join(d, "figures"))
        data = os.path.join(d, "models", "out.csv")
        png = os.path.join(d, "figures", "b.png")
        sp = png + ".provenance.json"
        open(data, "w").write("x,y\n1,2\n")
        first = (f"import sys, os\nsys.path.insert(0, {here!r})\nimport figure_validator as fv\n"
                 "d = os.getcwd()\n"
                 "open(os.path.join(d, 'figures', 'b.png'), 'w').write(open(os.path.join(d, 'models', 'out.csv')).read())\n"
                 "fv.validate_figure(os.path.join(d, 'figures', 'b.png'), [os.path.join(d, 'models', 'out.csv')],"
                 " expected_annotations=['Cases by unit'])\n# title: Cases by unit\n")
        open(os.path.join(d, "models", "eda.py"), "w").write(first)
        r = subprocess.run([sys.executable, os.path.join(d, "models", "eda.py")], cwd=d, capture_output=True, text=True)
        ok(r.returncode == 0 and os.path.exists(sp), f"DR13 fixture: the first draw writes a sidecar: {r.stderr[-300:]}")
        # The script is then edited to save without validate_figure (eda.py's pre-fix shape).
        redraw = ("import os\nd = os.getcwd()\n# title: Cases by unit\n"
                  "open(os.path.join(d, 'figures', 'b.png'), 'w').write(open(os.path.join(d, 'models', 'out.csv')).read())\n")
        open(os.path.join(d, "models", "eda.py"), "w").write(redraw)
        sc0 = json.load(open(sp))

        class _Rec:
            def __init__(self):
                self.w = []

            def harness_wrote(self, rel):
                self.w.append(rel)
        rec = _Rec()
        open(data, "w").write("x,y\n1,3\n")
        rep = rebuild(d, integrity=rec)
        sc1 = json.load(open(sp))
        ok(rep["still_stale"] == [] and rep["rebuilt"] == ["figures/b.png"]
           and rep["provenance_rerecorded"] == ["figures/b.png"] and fv.check_staleness(png)["status"] == "fresh"
           and "provenance re-recorded by the harness for 1" in summary_line(rep),
           f"DR13 a generator that saves without validate_figure is fresh after the rebuild: {rep}")
        ok(sc1.get("provenance_by") == PROVENANCE_BY and sc1.get("provenance_rerecorded_at_utc")
           and all(sc1.get(k) == sc0.get(k) for k in ("generator_script", "png_path", "expected_annotations",
                                                       "written_at_utc", "expected_n_data_points")),
           f"DR13b the re-recorded sidecar keeps its other fields and says the harness wrote it: {sc1}")
        ok(rec.w == ["figures/b.png.provenance.json"], f"DR13c the integrity record is told the harness wrote it: {rec.w}")
        # A generator that exits 0 without touching the PNG: the figure stays stale.
        open(os.path.join(d, "models", "eda.py"), "w").write("print('nothing drawn')\n")
        open(data, "w").write("x,y\n1,4\n")
        rep = rebuild(d)
        ok(rep["still_stale"] == ["figures/b.png"] and rep["provenance_rerecorded"] == []
           and "did not rewrite" in rep["provenance_not_rerecorded"][0]["why"],
           f"DR14 a generator that does not rewrite the PNG leaves it stale: {rep}")
        # A generator that rewrites the PNG, then fails: stale.
        open(os.path.join(d, "models", "eda.py"), "w").write(redraw + "raise SystemExit('boom')\n")
        rep = rebuild(d)
        ok(rep["still_stale"] == ["figures/b.png"] and rep["provenance_rerecorded"] == []
           and rep["runs"][0]["returncode"] == 1, f"DR15 a failed generator leaves its figure stale: {rep}")
        # A recorded annotation the script no longer draws: stale (validate_figure's own rule).
        open(os.path.join(d, "models", "eda.py"), "w").write(redraw.replace("Cases by unit", "Deaths"))
        rep = rebuild(d)
        ok(rep["still_stale"] == ["figures/b.png"] and "annotation" in rep["provenance_not_rerecorded"][0]["why"],
           f"DR16 a recorded annotation missing from the script keeps the figure stale: {rep}")
        # A recorded source that no longer exists: stale.
        open(os.path.join(d, "models", "eda.py"), "w").write(redraw.replace("models', 'out.csv", "models', 'gone.csv")
                                                              .replace("open(os.path.join(d, 'models', 'gone.csv')).read()", "'x'"))
        os.rename(data, data + ".bak")
        rep = rebuild(d)
        ok(rep["still_stale"] == ["figures/b.png"] and "source missing" in rep["provenance_not_rerecorded"][0]["why"],
           f"DR17 a missing recorded source keeps the figure stale: {rep}")
        os.rename(data + ".bak", data)
        # A script that rewrites the sidecar itself is the script's record, not re-recorded by the harness.
        open(os.path.join(d, "models", "eda.py"), "w").write(
            redraw + f"import json\np = os.path.join(d, 'figures', 'b.png.provenance.json')\n"
                     "s = json.load(open(p)); s['note'] = 'x'; json.dump(s, open(p, 'w'))\n")
        rep = rebuild(d)
        ok(rep["still_stale"] == ["figures/b.png"] and "sidecar itself" in rep["provenance_not_rerecorded"][0]["why"],
           f"DR18 a script that rewrote its own sidecar is not overridden: {rep}")
    # Phase 47 α Codex round 1 (P1s): sources rewritten during the run, a later script
    # overwriting a promoted figure, symlinks, malformed annotations.
    def _fig(d, name, src, ann=None):
        """A first draw through validate_figure (writes the sidecar), then the script is
        replaced by one that redraws the PNG without it. Returns the script path."""
        s = os.path.join(d, "models", f"draw_{name}.py")
        open(s, "w").write(
            f"import sys, os\nsys.path.insert(0, {here!r})\nimport figure_validator as fv\nd = os.getcwd()\n"
            f"open(os.path.join(d, 'figures', '{name}.png'), 'w').write(open({src!r}).read())\n"
            f"fv.validate_figure(os.path.join(d, 'figures', '{name}.png'), [{src!r}])\n")
        r = subprocess.run([sys.executable, s], cwd=d, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        if ann is not None:
            spf = os.path.join(d, "figures", f"{name}.png.provenance.json")
            j = json.load(open(spf)); j["expected_annotations"] = ann; json.dump(j, open(spf, "w"))
        return s

    redraw_tpl = ("import os\nd = os.getcwd()\n"
                  "open(os.path.join(d, 'figures', '{name}.png'), 'w').write(open({src!r}).read() + '{tag}')\n")
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        os.makedirs(os.path.join(d, "models")); os.makedirs(os.path.join(d, "figures"))
        src = os.path.join(d, "models", "src.csv")
        open(src, "w").write("x\n1\n")
        s = _fig(d, "r", src)
        # DR19: reads its source, draws, then rewrites that source before exiting 0.
        open(s, "w").write(redraw_tpl.format(name="r", src=src, tag="") + f"open({src!r}, 'a').write('9\\n')\n")
        open(src, "a").write("2\n")
        rep = rebuild(d)
        ok(rep["still_stale"] == ["figures/r.png"] and rep["provenance_rerecorded"] == []
           and "changed while its script ran" in rep["provenance_not_rerecorded"][0]["why"],
           f"DR19 (Codex P1) a script that rewrites its own source is not certified fresh: {rep}")
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        os.makedirs(os.path.join(d, "models")); os.makedirs(os.path.join(d, "figures"))
        src = os.path.join(d, "models", "src.csv")
        open(src, "w").write("x\n1\n")
        sa = _fig(d, "a", src)
        time.sleep(0.01)
        sb = _fig(d, "b", src)
        # DR20: A redraws a.png; B (run after A) overwrites a.png, then fails.
        open(sa, "w").write(redraw_tpl.format(name="a", src=src, tag=""))
        open(sb, "w").write(redraw_tpl.format(name="a", src=src, tag="B") + "raise SystemExit(3)\n")
        open(src, "a").write("2\n")
        rep = rebuild(d)
        why = {x["figure"]: x["why"] for x in rep["provenance_not_rerecorded"]}
        ok([r["returncode"] for r in rep["runs"]] == [0, 3] and "figures/a.png" in rep["still_stale"]
           and "later script" in why.get("figures/a.png", ""),
           f"DR20 (Codex P1) a figure a later failing script overwrote stays stale: {rep}")
        # DR22: malformed expected_annotations on one figure: it stays stale with a reason,
        # and the other script still runs and its figure is re-recorded.
        spa = os.path.join(d, "figures", "a.png.provenance.json")
        j = json.load(open(spa)); j["expected_annotations"] = 1; json.dump(j, open(spa, "w"))
        open(sb, "w").write(redraw_tpl.format(name="b", src=src, tag=""))
        open(src, "a").write("3\n")
        rep = rebuild(d)
        why = {x["figure"]: x["why"] for x in rep["provenance_not_rerecorded"]}
        ok(rep["still_stale"] == ["figures/a.png"] and rep["provenance_rerecorded"] == ["figures/b.png"]
           and "not a list of strings" in why.get("figures/a.png", ""),
           f"DR22 (Codex P1) malformed annotations stay stale and stop nothing else: {rep}")
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        d = os.path.realpath(d)
        os.makedirs(os.path.join(d, "models")); os.makedirs(os.path.join(d, "figures"))
        src = os.path.join(d, "models", "src.csv")
        open(src, "w").write("x\n1\n")
        s = _fig(d, "l", src)
        # DR21: the PNG is a symlink to a file outside the run: no harness write.
        png = os.path.join(d, "figures", "l.png")
        os.replace(png, os.path.join(out, "l.png"))
        os.symlink(os.path.join(out, "l.png"), png)
        open(s, "w").write(redraw_tpl.format(name="l", src=src, tag=""))
        open(src, "a").write("2\n")
        rep = rebuild(d)
        ok(rep["still_stale"] == ["figures/l.png"] and "symlink" in rep["provenance_not_rerecorded"][0]["why"],
           f"DR21 (Codex P1) a symlinked PNG is not re-recorded: {rep}")
        # DR21b: a symlinked sidecar pointing outside: refused, nothing written outside.
        os.unlink(png); open(png, "w").write("x")
        sp = png + ".provenance.json"
        os.replace(sp, os.path.join(out, "l.json")); os.symlink(os.path.join(out, "l.json"), sp)
        before_out = open(os.path.join(out, "l.json")).read()
        rep = rebuild(d)
        ok(rep["provenance_rerecorded"] == [] and open(os.path.join(out, "l.json")).read() == before_out,
           f"DR21b (Codex P1) a symlinked sidecar is not written through: {rep}")
    # (pipeline review) scripts/pipeline.py is loaded by path, not by a plain import that
    # can resolve to a run's own models/pipeline.py.
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "pipeline.py"), "w").write("SPEC = 'run-module'\n")
        code = (f"import sys; sys.path.insert(0, {d!r}); import pipeline; "
                f"sys.path.insert(0, {here!r}); import derived_rebuild as dr; "
                "print(dr.pipeline.__file__, dr.pipeline.SPEC, sys.modules['pipeline'].SPEC)")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=d)
        ok(r.returncode == 0 and os.path.realpath(r.stdout.split()[0]) == os.path.realpath(os.path.join(here, "pipeline.py"))
           and r.stdout.split()[1:] == ["models/pipeline.yaml", "run-module"],
           f"DR12 (pipeline review) derived_rebuild loads scripts/pipeline.py by path beside a run's pipeline "
           f"module: {r.stdout}{r.stderr[-300:]}")
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
