#!/usr/bin/env python3
"""World-model trials (F6): the world model (toolkit/analysis/world_model.py) on many low-compute questions.

Not yet implemented: the self-tests below define the behaviour (α).
"""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class TrialError(ValueError):
    """An input the trials cannot use, or a refused action (budget, push scope): fails closed with the reason."""


def load_questions(path, root=REPO, engines=None):
    raise NotImplementedError


class SpendLedger:
    def __init__(self, path, total_cap_usd):
        raise NotImplementedError


class GitAnchor:
    def __init__(self, repo, worktree, branch="wm-trials", remote="origin", trials_rel="trials"):
        raise NotImplementedError


def run_trials(*a, **k):
    raise NotImplementedError


def score(*a, **k):
    raise NotImplementedError


def seed_problems(*a, **k):
    raise NotImplementedError


# ---------------------------------------------------------------- self-test
def _toy_template(d, points=12):
    """A trial template on the toy engine (the stub engine of these tests): the analysis self-tests' toy spec."""
    import yaml
    from toolkit.spec import schema
    os.makedirs(d, exist_ok=True)
    p = schema.toy_spec(d)
    raw = yaml.safe_load(open(p))
    raw["analysis"]["emulator"]["points"] = points
    with open(p, "w") as f:
        yaml.safe_dump(raw, f, sort_keys=False)
    return d


def _toy_seed(bad=None):
    """A valid seed on the toy sweep's names (packages none, a, b, a+b; features 'x (calibration scale)', eff_a,
    eff_b); `bad` plants a defect: 'schema' (a test without its threshold) or 'name' (a feature the sweep lacks)."""
    lit = [{"citation": "Toy et al. (2020) J Toy 1:1", "location": "Table 2"}]
    sd = {"beliefs": [
        {"id": "B1", "mechanism": "a reduces the outcome", "about": {"package": "a"}, "direction": "reduces",
         "magnitude": {"band": [0.2, 0.8], "scale": "relative_reduction"}, "source_type": "literature",
         "evidence": lit, "confidence": "high", "decision_relevant": True},
        {"id": "B2", "mechanism": "b reduces the outcome less than a", "about": {"package": "b"},
         "direction": "reduces", "magnitude": {"band": [0.1, 0.5], "scale": "relative_reduction"},
         "source_type": "literature", "evidence": lit, "confidence": "low"},
        {"id": "B3", "mechanism": "a's effect grows with its efficacy", "about": {"package": "a", "feature": "eff_a"},
         "direction": "increases", "source_type": "literature", "evidence": lit, "confidence": "medium"},
        {"id": "B4", "mechanism": "a beats b at the shadow price", "about": {"pair": ["a", "b"]},
         "direction": "flips", "source_type": "literature", "evidence": lit, "confidence": "low",
         "decision_relevant": True}],
        "open_questions": [{"id": "Q1", "question": "does b's effect depend on x?", "beliefs": ["B2"],
                            "decision_relevant": True}],
        "hypotheses": [
            {"id": "H1", "belief": "B1", "claim": "a reduces by 20-80%",
             "test": {"kind": "effect_band", "package": "a", "band": [0.2, 0.8], "min_share": 0.7}},
            {"id": "H2", "belief": "B2", "claim": "b reduces by 10-50%",
             "test": {"kind": "effect_band", "package": "b", "band": [0.1, 0.5], "min_share": 0.7}},
            {"id": "H3", "belief": "B3", "claim": "a's reduction increases with eff_a",
             "test": {"kind": "effect_direction", "package": "a", "feature": "eff_a", "direction": "increases",
                      "scope": "marginal"}},
            {"id": "H4", "belief": "B4", "claim": "a beats b across x",
             "test": {"kind": "paired_sign", "a": "a", "b": "b", "feature": "x (calibration scale)",
                      "range": [-2.0, 2.0], "sign": "positive", "min_share": 0.7}},
            {"id": "H5", "belief": "B1", "claim": "a reduces by at least a tenth almost everywhere",
             "test": {"kind": "effect_band", "package": "a", "band": [0.1, 1.0], "min_share": 0.9}}]}
    if bad == "schema":
        del sd["hypotheses"][0]["test"]["min_share"]
    if bad == "name":
        sd["hypotheses"][2]["test"]["feature"] = "no_such_feature"
        sd["beliefs"][2]["about"]["feature"] = "no_such_feature"
    return sd


def _git(*a, cwd):
    import subprocess
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(a)}: {r.stderr.strip()}")
    return r.stdout.strip()


def _toy_repo(d):
    """A repository with a bare origin (no network): trials/ ignored on its main branch, as in the real repo."""
    origin, repo = os.path.join(d, "origin.git"), os.path.join(d, "repo")
    os.makedirs(repo)
    _git("init", "--bare", "-q", origin, cwd=d)
    _git("init", "-q", "-b", "main", cwd=repo)
    for k, v in (("user.name", "Test"), ("user.email", "test@example.org"), ("commit.gpgsign", "false")):
        _git("config", k, v, cwd=repo)
    with open(os.path.join(repo, ".gitignore"), "w") as f:
        f.write("trials/\n")
    _git("add", ".gitignore", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("remote", "add", "origin", origin, cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)
    return repo


def _self_test() -> int:
    import tempfile
    import threading
    import time
    import yaml
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def tryit(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- the failure is the finding
            ok(False, f"{name}: {type(e).__name__}: {e}")

    toy_ref = "toolkit.engines.contract:ToyEngine"

    def wt1():
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "spend.jsonl")
            L = SpendLedger(p, 5.0)
            r1 = L.reserve("q1", "seed", 2.0)
            L.charge(r1, 0.75, "sdk reported")
            r2 = L.reserve("q2", "seed", 2.0)              # outstanding: counted at its whole cap
            ok(r1 and r2 and abs(L.committed() - 2.75) < 1e-9 and abs(L.spent() - 0.75) < 1e-9,
               f"WT1 an outstanding reservation counts at its cap, a charged one at its charge: committed "
               f"{L.committed()}, spent {L.spent()}")
            L2 = SpendLedger(p, 5.0)                       # a later invocation reads the same ledger
            ok(L2.reserve("q3", "seed", 2.5) is None and L2.reserve("q3", "seed", 2.0) is not None,
               "WT1b the cap holds across invocations: a task whose cap would take committed spend past the total is "
               "refused, one that fits is reserved")
            lines = open(p).read().splitlines()
            bad = json.loads(lines[1])
            bad["usd"] = 0.01
            lines[1] = json.dumps(bad, sort_keys=True)
            with open(p, "w") as f:
                f.write("\n".join(lines) + "\n")
            try:
                SpendLedger(p, 5.0).committed()
                ok(False, "WT1c an edited spend ledger line is refused (append-only chain)")
            except TrialError:
                ok(True, "WT1c an edited spend ledger line is refused (append-only chain)")
            try:
                SpendLedger(os.path.join(d, "x.jsonl"), 0)
                ok(False, "WT1d a total cap that is not a positive number is refused")
            except TrialError:
                ok(True, "WT1d a total cap that is not a positive number is refused")
    tryit("WT1 spend ledger", wt1)

    def wt2():
        with tempfile.TemporaryDirectory() as d:
            _toy_template(os.path.join(d, "tpl", "toy"))
            qp = os.path.join(d, "q.yaml")
            doc = {"questions": [{"id": "toy1", "question": "Which package per unit?", "engine": toy_ref,
                                  "template": "tpl/toy", "shadow_price": 0.1, "inputs": "synthetic"}]}
            yaml.safe_dump(doc, open(qp, "w"))
            got = load_questions(qp, root=d, engines={toy_ref})
            ok([q["id"] for q in got] == ["toy1"], f"WT2 a questions file loads: {[q['id'] for q in got]}")
            for mut, why in ((lambda q: q.update(engine="toolkit.engines.malaria.engine:MalariaEngine"),
                              "an engine that is not one of the cheap engines"),
                             (lambda q: q.update(template="tpl/none"), "a missing template"),
                             (lambda q: q.pop("question"), "no question text"),
                             (lambda q: q.update(id="a b/c"), "an id that is not a plain name")):
                bad = json.loads(json.dumps(doc))
                mut(bad["questions"][0])
                yaml.safe_dump(bad, open(qp, "w"))
                try:
                    load_questions(qp, root=d, engines={toy_ref})
                    ok(False, f"WT2b refused: {why}")
                except TrialError:
                    ok(True, f"WT2b refused: {why}")
            dup = {"questions": doc["questions"] * 2}
            yaml.safe_dump(dup, open(qp, "w"))
            try:
                load_questions(qp, root=d, engines={toy_ref})
                ok(False, "WT2b refused: a duplicate id")
            except TrialError:
                ok(True, "WT2b refused: a duplicate id")
        real = load_questions(os.path.join(REPO, "scripts", "world_model_trials_questions.yaml"))
        evals = [q for q in real if q.get("eval_question")]
        ok(len(real) >= 10 and len(evals) == 5 and len({q["engine"] for q in real}) >= 3,
           f"WT2c the shipped questions file: {len(real)} questions, {len(evals)} from the eval suite, engines "
           f"{sorted({q['engine'].split(':')[-1] for q in real})}")
    tryit("WT2 questions", wt2)

    def e2e(d, seeds, cap=20.0, task_cap=2.0, n=1, push=True):
        """One trials run on toy questions: (repo, trials root, results, calls, ledger)."""
        repo = _toy_repo(d)
        for i in range(n):
            _toy_template(os.path.join(repo, "data", "tpl", f"toy{i}"))
        qs = [{"id": f"toy{i}", "question": "Which package per unit?", "engine": toy_ref,
               "template": f"data/tpl/toy{i}", "shadow_price": 0.1, "inputs": "synthetic"} for i in range(n)]
        calls, live = [], {"llm": 0, "llm_max": 0, "eng": 0, "eng_max": 0}
        lock = threading.Lock()

        def seed_fn(run_dir, ctx, feedback, cap_usd):
            from agents.v2_tasks import TaskResult
            with lock:
                live["llm"] += 1
                live["llm_max"] = max(live["llm_max"], live["llm"])
            try:
                calls.append({"run": os.path.basename(run_dir), "feedback": feedback, "cap": cap_usd,
                              "ctx": ctx})
                k = sum(c["run"] == os.path.basename(run_dir) for c in calls)
                sd = seeds(os.path.basename(run_dir), k)
                with open(os.path.join(run_dir, "world_model_seed.yaml"), "w") as f:
                    yaml.safe_dump(sd, f, sort_keys=False)
                time.sleep(0.05)
                return TaskResult(True, {"ok": True}, 0.25, "stub")
            finally:
                with lock:
                    live["llm"] -= 1

        def engine_probe(phase):
            with lock:
                live["eng"] += 1 if phase == "start" else -1
                live["eng_max"] = max(live["eng_max"], live["eng"])
        troot = os.path.join(repo, "trials")
        L = SpendLedger(os.path.join(troot, "spend.jsonl"), cap)
        anchor = GitAnchor(repo, os.path.join(troot, ".wm-trials")) if push else None
        res = run_trials(qs, troot, repo=repo, seed_fn=seed_fn, ledger=L, anchor=anchor, task_cap_usd=task_cap,
                         llm_concurrency=2, engine_workers=2, max_engine_runs=50, engines={toy_ref},
                         engine_probe=engine_probe)
        return repo, troot, res, calls, L, live

    def wt3():
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, live = e2e(d, lambda q, k: _toy_seed())
            r = res["toy0"]
            rd = os.path.join(troot, "runs", "toy0")
            hy = yaml.safe_load(open(os.path.join(rd, "analysis", "hypotheses.yaml")))
            regs = {h["id"]: h["confrontation"]["registration_status"] for h in hy["hypotheses"]}
            ok(r["status"] == "complete" and set(regs.values()) == {"remote-anchored"},
               f"WT3 one question end to end (stub seed, toy engine, local bare origin): {r['status']}, every "
               f"hypothesis remote-anchored before its evidence: {regs}")
            tree = _git("ls-tree", "-r", "--name-only", "refs/remotes/origin/wm-trials", cwd=repo).splitlines()
            ok(sorted(tree) == ["trials/runs/toy0/analysis/hypotheses_registry.jsonl",
                                "trials/runs/toy0/analysis/world_model_seed.yaml"],
               f"WT3b the wm-trials branch holds the ledger and the seed only, never run data: {tree}")
            ok(_git("status", "--porcelain", cwd=repo) == "" and
               _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "main",
               "WT3c the main working tree is untouched (the wm-trials branch lives in its own worktree)")
            ctx = calls[0]["ctx"]
            from toolkit.analysis import world_model as W
            sw, _ = W.load_sweep(rd, "current", 0.1, use_plan=False)
            ok(sorted(ctx["sweep"]["features"]) == sorted(sw.features) and ctx["packages"] == sw.packages,
               f"WT3d the seed task is told the sweep's own feature and package names (world_model's, from the "
               f"design, before any result): {sorted(ctx['sweep']['features'])}")
            ok(not any("result" in k.lower() for k in json.dumps(ctx).split('"') if k.endswith(".jsonl")),
               "WT3e the seed context names no results file")
            first = [json.loads(x) for x in open(os.path.join(rd, "analysis", "batch", "first_seen.jsonl"))]
            ok(len(first) == 1 and r["spend_usd"] == 0.25 and abs(L.spent() - 0.25) < 1e-9,
               f"WT3f the batch's first-seen time is recorded once by the driver, and spend is per question "
               f"({r['spend_usd']}) and in the ledger ({L.spent()})")
            wm = yaml.safe_load(open(os.path.join(rd, "analysis", "world_model.yaml")))
            ok(wm["track_record"]["registry"]["anchors"] and r["engine"]["jobs"] == 48,
               f"WT3g the build reads the remote anchor; the sweep ran {r['engine']['jobs']} engine jobs")
    tryit("WT3 end to end", wt3)

    def wt4():
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, _ = e2e(d, lambda q, k: _toy_seed("schema" if k == 1 else None))
            fb = calls[1]["feedback"] if len(calls) > 1 else None
            ok(len(calls) == 2 and fb and any("min_share" in x for x in fb) and res["toy0"]["status"] == "complete"
               and res["toy0"]["seed"]["repairs"] == 1,
               f"WT4 a seed the world model's validator refuses is repaired once with the validator's errors: "
               f"{fb}")
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, _ = e2e(d, lambda q, k: _toy_seed("schema"))
            reg = os.path.join(troot, "runs", "toy0", "analysis", "hypotheses_registry.jsonl")
            ok(len(calls) == 2 and res["toy0"]["status"] == "seed_refused" and not os.path.exists(reg),
               f"WT4b refused twice: the question stops, nothing is registered ({res['toy0']['status']})")
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, _ = e2e(d, lambda q, k: _toy_seed("name" if k == 1 else None))
            fb = calls[1]["feedback"] if len(calls) > 1 else []
            ok(len(calls) == 2 and any("no_such_feature" in x for x in fb or []),
               f"WT4c a test naming a feature or package the sweep does not have is repaired: {fb}")
    tryit("WT4 repair", wt4)

    def wt5():
        with tempfile.TemporaryDirectory() as d:
            repo = _toy_repo(d)
            A = GitAnchor(repo, os.path.join(repo, "trials", ".wm-trials"))
            os.makedirs(os.path.join(repo, "trials", "runs", "q1", "analysis", "batch"))
            for rel in ("trials/runs/q1/analysis/batch/results.jsonl", "trials/runs/q1/decision_spec.yaml"):
                with open(os.path.join(repo, rel), "w") as f:
                    f.write("{}\n")
                try:
                    A.publish([rel], "q1")
                    ok(False, f"WT5 publishing {rel} is refused (ledgers and seeds only)")
                except TrialError:
                    ok(True, f"WT5 publishing {rel} is refused (ledgers and seeds only)")
            try:
                A.publish(["trials/runs/q1/analysis/../../../../.gitignore"], "q1")
                ok(False, "WT5b a path that climbs out of the trials tree is refused")
            except TrialError:
                ok(True, "WT5b a path that climbs out of the trials tree is refused")
    tryit("WT5 push scope", wt5)

    def wt6():
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, live = e2e(d, lambda q, k: _toy_seed(), n=3)
            ok(all(r["status"] == "complete" for r in res.values()) and live["llm_max"] <= 2 and live["eng_max"] == 1,
               f"WT6 three questions: LLM steps in parallel at most llm_concurrency ({live['llm_max']}), engine "
               f"steps one batch at a time on 2 worker processes ({live['eng_max']})")
            n0 = len(calls)
            L2 = SpendLedger(os.path.join(troot, "spend.jsonl"), 20.0)
            qs = [{"id": f"toy{i}", "question": "Which package per unit?", "engine": toy_ref,
                   "template": f"data/tpl/toy{i}", "shadow_price": 0.1, "inputs": "synthetic"} for i in range(3)]
            res2 = run_trials(qs, troot, repo=repo, seed_fn=lambda *a: (_ for _ in ()).throw(AssertionError("called")),
                              ledger=L2, anchor=GitAnchor(repo, os.path.join(troot, ".wm-trials")),
                              engines={toy_ref})
            ok(len(calls) == n0 and all(r["status"] == "complete" and r.get("skipped") for r in res2.values()),
               "WT6b a second invocation skips completed questions (no LLM call, no engine run)")
    tryit("WT6 concurrency and resume", wt6)

    def wt7():
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, _ = e2e(d, lambda q, k: _toy_seed(), cap=2.5, task_cap=2.0, n=2, push=False)
            st = sorted(r["status"] for r in res.values())
            ok(st == ["budget_refused", "complete"] and len(calls) == 1,
               f"WT7 with a total cap of $2.50 and a $2 task cap, the second question is not launched: {st}")
            ok(all(h["confrontation"]["registration_status"] == "post hoc" for h in yaml.safe_load(open(os.path.join(
                troot, "runs", [k for k, r in res.items() if r["status"] == "complete"][0], "analysis",
                "hypotheses.yaml")))["hypotheses"]),
               "WT7b without a push the registrations are not anchored: every confrontation is post hoc")
    tryit("WT7 budget stop", wt7)

    def wt8():
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, _ = e2e(d, lambda q, k: _toy_seed(), n=1)
            S = score(troot)
            q = S["questions"]["toy0"]
            hy = yaml.safe_load(open(os.path.join(troot, "runs", "toy0", "analysis", "hypotheses.yaml")))
            st = {}
            for h in hy["hypotheses"]:
                st[h["status"]] = st.get(h["status"], 0) + 1
            ok(q["hypotheses"]["written"] == 5 and q["hypotheses"]["by_status"] == st
               and q["hypotheses"]["post_hoc"] == 0,
               f"WT8 the score reads the build's own statuses: {q['hypotheses']}")
            ok(set(S["overall"]) >= {"hypotheses", "confidence", "failure_modes", "where_to_look", "spend_usd"}
               and os.path.isfile(os.path.join(troot, "summary.yaml"))
               and os.path.isfile(os.path.join(troot, "summary.md")),
               "WT8b overall scores and the failure-mode taxonomy are written to trials/summary.yaml and summary.md")
            ok(isinstance(q["where_to_look"].get("decision_relevant"), bool),
               f"WT8c where_to_look's top item is classified as decision-relevant or not: {q['where_to_look']}")
        # planted outcomes: a lenient band, a high-confidence refutation and an untestable test are classified
        fake = {"hypotheses": [
            {"id": "H1", "belief": "B1", "status": "supported", "test": {"kind": "effect_band", "package": "a",
                                                                          "band": [0.0, 0.95], "min_share": 0.55},
             "result": {"observed": {"coverage": 1.0}}, "confrontation": {"post_hoc": False}},
            {"id": "H2", "belief": "B2", "status": "refuted", "test": {"kind": "effect_band", "package": "b",
                                                                        "band": [0.4, 0.5], "min_share": 0.8},
             "result": {"observed": {"coverage": 0.0}}, "confrontation": {"post_hoc": False}},
            {"id": "H3", "belief": "B1", "status": "untestable", "test": {"kind": "plan_share", "package": "a",
                                                                           "where": {"x": [0, 1]}, "min_share": 0.5},
             "result": {"observed": {"reason": "no optimised plan (analysis/point.yaml)"}},
             "confrontation": {"post_hoc": False}}]}
        seed = {"beliefs": [{"id": "B1", "confidence": "low", "source_type": "literature",
                             "evidence": [{"citation": "Someone", "location": "p. 3"}]},
                            {"id": "B2", "confidence": "high", "source_type": "literature",
                             "evidence": [{"citation": "Doe J et al. Lancet 2019;393:1-9", "location": "Table 2"}]}],
                "hypotheses": fake["hypotheses"]}
        fm = failure_modes(fake["hypotheses"], seed, {"beliefs": seed["beliefs"], "open_questions": []})
        ok(fm.get("test_too_lenient") == ["H1"] and fm.get("untestable") == ["H3"]
           and fm.get("high_confidence_refuted") == ["H2"] and fm.get("citation_not_verifiable") == ["B1"]
           and fm.get("contradiction_without_open_question") == ["H2"],
           f"WT8d the failure-mode taxonomy classifies planted outcomes: {fm}")
    tryit("WT8 scoring", wt8)

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def failure_modes(*a, **k):
    raise NotImplementedError


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    sys.exit(2)
