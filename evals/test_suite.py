"""Question-specific self-tests of scripts/eval_suite.py (Phase 50 α).

They hold expected answers and fixtures for the eval questions, so they live here,
under the export-ignored evals/ tree, and never reach agents. `eval_suite.py
--self-test` runs them when this file is present (a checkout), and skips them in a
shipped tree.
"""
import csv
import json
import os
import shutil
import subprocess
import sys


def run(ok, td, es):
    """es: the eval_suite module. Every case reports through ok(cond, msg)."""
    g = globals()
    g.update({k: v for k, v in vars(es).items() if not k.startswith("__")})
    real_tax = load_taxonomy()
    codes_used = {"run_record_missing", "run_failed", "no_report", "false_confidence_accept",
                  "confident_wrong_interval", "grader_error", "inputs_not_referenced", "allocation_missing",
                  "allocation_incomplete", "allocation_discordant", "fell_for_trap", "allocation_infeasible", "inputs_mismatch", "inputs_unrecorded",
                  "plan_value_low", "unit_estimates_off", "headline_missing", "headline_off",
                  "interval_misses_truth", "interval_uninformative", "mechanism_not_reported",
                  "scope_not_declared", "false_precision", "statement_missing",
                  "gate_outcome_unexpected"}
    ok(codes_used <= set(real_tax["failures"]), f"Q0 taxonomy covers every failure code: "
                                                f"{codes_used - set(real_tax['failures'])}")
    layer_ids = [x["id"] for x in real_tax["layers"]]
    ok(all(v["layer"] in layer_ids for v in real_tax["failures"].values()), "Q0b taxonomy layers are declared")
    for q in suite_questions():
        rub = load_rubric(q)
        ok(abs(sum(c.get("weight", 0) for c in rub["checks"]) - 1.0) < 1e-9, f"Q1 {q} weights sum to 1")
        ok(all(c["type"] in CHECK_TYPES for c in rub["checks"]), f"Q1b {q} check types known")
        sids = {s["id"] for s in rub["subtasks"]}
        ok(all(c["subtask"] in sids for c in rub["checks"]), f"Q1c {q} checks hang off declared subtasks")
        gen = load_generator(q)
        d1, d2, d3 = (os.path.join(td, q, x) for x in ("a", "b", "c"))
        t1 = gen.generate(os.path.join(d1, "in"), os.path.join(d1, "_truth"))
        gen.generate(os.path.join(d2, "in"), os.path.join(d2, "_truth"))
        gen.generate(os.path.join(d3, "in"), os.path.join(d3, "_truth"), seed=gen.SEED + 1, strict=False)
        ok(digest_dir(os.path.join(d1, "in")) == digest_dir(os.path.join(d2, "in"))
           and open(os.path.join(d1, "_truth", "truth.json")).read()
           == open(os.path.join(d2, "_truth", "truth.json")).read(), f"Q2 {q} same seed, same data and truth")
        ok(digest_dir(os.path.join(d1, "in")) != digest_dir(os.path.join(d3, "in")),
           f"Q2b {q} another seed gives other data")
        ok(digest_dir(os.path.join(d1, "in")) == digest_dir(os.path.join(DATA, q)),
           f"Q2c {q} committed data/evals/{q}/ matches the generator (run `eval_suite.py generate {q}`)")
        for kind in ("perfect", "wrong"):
            rd = os.path.join(td, q, kind)
            build_fixture(rd, rub, t1, kind)
            s = score(extract_answer(rd, rub, t1), rub, t1, read_texts(rd, rub.get("text_files")),
                      read_all_records(rd), sorted(t1["inputs_sha256"]), real_tax)
            codes = {f["code"] for f in s["failures"]}
            if kind == "perfect":
                ok(s["quality"] >= 0.9 and s["quality_gated"] >= 0.9 and not s["penalties"],
                   f"Q3 {q} hand-built perfect answer scores high: {s['quality']} {s['quality_gated']} "
                   f"{[(c['id'], c['score'], c['detail']) for c in s['checks'] if not c['passed']]}")
            else:
                ok(s["quality"] <= 0.3 and "false_confidence_accept" in codes,
                   f"Q4 {q} planted wrong answer scores low with a false-confidence penalty: "
                   f"{s['quality']} {sorted(codes)}")
                if t1.get("allocation"):
                    ok("fell_for_trap" in codes, f"Q4b {q} the naive optimum is named as the trap: {codes}")
                ok(s["primary_layer"] in layer_ids, f"Q4c {q} a primary layer is named: {s['primary_layer']}")
    # configs: one per question, small and capped
    sys.path.insert(0, os.path.join(REPO, "cloud"))
    import run_cluster
    for q in suite_questions():
        cfg = run_cluster.load_config(os.path.join(REPO, "cloud", "configs", f"eval_{q}.py"))
        fl = list(cfg.flags)
        ok(fl[fl.index("--max-budget-usd") + 1] == "35" and cfg.max_rounds <= 3
           and os.path.isfile(cfg.question_file) and f"data/evals/{q}/" in " ".join(cfg.required_files)
           and cfg.out_dir.endswith(f"evals/main/{q}"), f"C1 {q} config: cap 35, <=3 rounds, inputs required")
        qtext = open(cfg.question_file).read()
        ok("SYNTHETIC" in qtext and all(f"data/evals/{q}/{n}" in qtext for n in os.listdir(os.path.join(DATA, q))),
           f"C2 {q} question is labelled synthetic and names every input file")
    # leaks: the shipped tree and the harness manifest never hold hidden eval files
    ok(not eval_paths.hidden_members(_archive_of(REPO)), "L1 git archive of HEAD ships no eval-suite file")
    ok(_attributes_hide(REPO), "L2 .gitattributes export-ignores evals/ and the design note")
    tracked = subprocess.run(["git", "-C", REPO, "ls-files"], capture_output=True, text=True).stdout.split()
    ok(not [p for p in tracked if "/_truth/" in f"/{p}"], "L3 no answer key is tracked by git")
    sys.path.insert(0, REPO)
    from agents import enforcement
    man = enforcement.hash_harness(REPO)
    ok(man and not [p for p in man if eval_paths.is_hidden(p)], "L4 the harness manifest lists no eval file")
    deny = lambda tool, inp: enforcement.check_tool_use(tool, inp, os.path.join(REPO, "runs", "r"), None, [],  # noqa: E731
                                                        cwd=REPO)
    ok(deny("Read", {"file_path": "evals/malaria_alloc/generate.py"})
       and deny("Read", {"file_path": os.path.join(REPO, "evals", "malaria_alloc", "_truth", "truth.json")})
       and deny("Grep", {"pattern": "x", "path": "evals"})
       and deny("Bash", {"command": "cat evals/tb_acf/rubric.yaml"})
       and deny("Bash", {"command": "python3 -c \"open('../../evals/q/_truth/truth.json')\""})
       and deny("Read", {"file_path": "docs/design/eval-suite.md"}), "L5 agents may not read eval files")
    ok(not deny("Read", {"file_path": "data/evals/malaria_alloc/districts.csv"})
       and not deny("Bash", {"command": "head data/evals/tb_acf/districts.csv"})
       and not deny("Bash", {"command": "python3 -c 'evals = [1]; print(evals)'"}),
       "L6 the shipped inputs stay readable and a variable named evals is not refused")
    for p in ("evals/q/generate.py", "evals/q/_truth/t.json", "data/evals/q/a.csv", "docs/design/eval-suite.md"):
        ok(eval_paths.is_hidden(p) == enforcement.is_eval_hidden(os.path.join(REPO, p)),
           f"L7 enforcement and eval_paths agree on {p}")
    for name, fn in (("R1", _review_r1_leaks), ("R2", _review_r2_workspace), ("R3", _review_r3_feasible),
                     ("R4", _review_r4_discovery), ("R5", _review_r5_inputs),
                     ("R6", _review_r6_numeric_leaks), ("R7", _review_r7_cloud_only), ("R8", _review_r8_resume),
                     ("R9", _review_r9_detection), ("R10", _review_r10_rows), ("R11", _review_r11_launch_id)):
        try:
            fn(ok, os.path.join(td, f"review_{name}"))
        except (Exception, SystemExit) as e:  # noqa: BLE001 — a missing piece is a failed case, not a crash
            ok(False, f"{name} raised {type(e).__name__}: {e}")


# ---- review round 1 (Phase 50 α, Codex P1s): each test reproduces the reported scenario --

def _write_alloc(rd, header, rows):
    with open(os.path.join(rd, "allocation_optimized.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _launch_fixture(out, q, truth, labels, hashes=None):
    hashes = hashes if hashes is not None else {f"data/evals/{q}/{n}": h for n, h in truth["inputs_sha256"].items()}
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "assignments.json"), "w") as f:
        json.dump({lab: {"launch_id": lid, "eval_inputs_sha256": hashes} for lab, lid in labels.items()}, f)


def _collected(label_dir, launch_id, rubric, truth, kind):
    build_fixture(os.path.join(label_dir, "runs", f"2026-11-01_{kind}"), rubric, truth, kind)
    with open(os.path.join(label_dir, "result.json"), "w") as f:
        json.dump({"launch_id": launch_id}, f)


def _review_r1_leaks(ok, td):
    hits = shipped_leaks(REPO)
    ok(not hits, f"R1 no shipped file signals a planted answer: {hits[:5]}")
    kw = sorted(load_rubric(suite_questions()[-1]).get("leak_keywords", ["?"]))[0]   # built here, never written out
    fake = {"CHANGELOG.md": f"| 50 | the eval suite: one question's answer is partly {kw} |"}
    ok(shipped_leaks(REPO, extra_texts=fake), "R1b a shipped line naming the suite beside a planted keyword is caught")
    ok(all(load_rubric(q).get("leak_keywords") for q in suite_questions()), "R1c every rubric lists its planted keywords")


def _review_r2_workspace(ok, td):
    q = open(os.path.join(EVALS, "malaria_alloc", "question.txt")).read()
    ok(eval_paths.eval_workspace_problem(q, REPO), "R2 an eval question is refused in the grader checkout")
    with open(os.path.join(REPO, "main.py")) as f:
        ok("eval_workspace_problem(" in f.read(), "R2d main.py applies the workspace rule before a run starts")


def _review_r3_feasible(ok, td):
    q = "flu_vaccine"
    rub, t = load_rubric(q), ensure_truth(q)

    def flu(name, doses):
        rd = os.path.join(td, name)
        build_fixture(rd, rub, t, "perfect")
        _write_alloc(rd, ["age_group", "doses", "hospitalisations_averted"], list(zip(t["allocation"]["units"], doses, [0] * 5)))
        return grade_run(q, rd, truth=t, verify_inputs=False)
    for name, doses in (("over", [0, 630000, 187500, 0, 0]),       # rounds to the optimum 0,8,2,0,0; 817,500 doses
                        ("cap", [300000, 450000, 0, 0, 0]),         # 0-4 above 70% uptake (245,000)
                        ("negative", [0, 600000, 225000, -75000, 0]),
                        ("partial", [0, 600000, 112500, 37500, 0])):  # not whole blocks
        s = flu(name, doses)
        alloc = {c["id"]: c for c in s["checks"] if c["type"].startswith("allocation_")}
        ok(all(c["score"] == 0 and "allocation_infeasible" in c["failures"] for c in alloc.values()),
           f"R3 flu {name} allocation {doses} gets no allocation credit: {[(c['id'], c['score'], c['detail']) for c in alloc.values()]}")
    q = "malaria_alloc"
    rub, t = load_rubric(q), ensure_truth(q)
    rd = os.path.join(td, "malaria_over")
    build_fixture(rd, rub, t, "perfect")
    al = t["allocation"]
    rich = {u: max(al["feasible"][u], key=lambda p: al["cost"][u][p]) for u in al["units"]}
    _write_alloc(rd, ["district", "package", "cases_averted"], [(u, p, al["benefit"][u][p]) for u, p in rich.items()])
    s = grade_run(q, rd, truth=t, verify_inputs=False)
    ok(all(c["score"] == 0 for c in s["checks"] if c["type"].startswith("allocation_") or c["type"] == "unit_value_accuracy"),
       f"R3b an over-budget categorical plan gets no allocation credit: {[(c['id'], c['score']) for c in s['checks']]}")


def _review_r4_discovery(ok, td):
    q = "malaria_alloc"
    rub, t = load_rubric(q), ensure_truth(q)
    out = os.path.join(td, q)
    _launch_fixture(out, q, t, {"R1": "L2", "R2": "L2", "R3": "L2"})
    _collected(os.path.join(out, "R1"), "L2", rub, t, "wrong")                 # the current, failed run
    _collected(os.path.join(out, "R1.before_L1_1793"), "L1", rub, t, "perfect")  # an earlier launch's success
    _collected(os.path.join(out, "R3"), "L1", rub, t, "perfect")               # R3 not yet re-collected
    rows = grade_root(td)["questions"][q]
    ok(len(rows) == 3 and rows[0]["quality"] == 0.0 and rows[0]["run_dir"] and ".before_" not in rows[0]["run_dir"]
       and rows[1]["run_dir"] is None and rows[1]["quality"] == 0.0 and rows[2]["quality"] == 0.0
       and all(r["label"] in ("R1", "R2", "R3") for r in rows),
       f"R4 labels come from assignments.json, backups are ignored, missing or stale replicates score 0: "
       f"{[(r.get('label'), r['quality'], r['run_dir'] and os.path.basename(os.path.dirname(os.path.dirname(r['run_dir'])))) for r in rows]}")
    ok(grade_root(os.path.join(td, "nothing"))["questions"][q][0]["failures"] == ["run_record_missing"],
       "R4b no launch record: the question scores 0")


def _review_r5_inputs(ok, td):
    q = "tb_acf"
    rub, t = load_rubric(q), ensure_truth(q)
    good = os.path.join(td, "good")
    _launch_fixture(os.path.join(good, q), q, t, {"R1": "L1"})
    _collected(os.path.join(good, q, "R1"), "L1", rub, t, "perfect")
    ok(grade_root(good)["questions"][q][0]["quality"] == 1.0, "R5 matching recorded input hashes: graded")
    bad = os.path.join(td, "bad")
    first = sorted(t["inputs_sha256"])[0]
    hashes = {f"data/evals/{q}/{n}": ("0" * 64 if n == first else h) for n, h in t["inputs_sha256"].items()}
    _launch_fixture(os.path.join(bad, q), q, t, {"R1": "L1"}, hashes=hashes)
    _collected(os.path.join(bad, q, "R1"), "L1", rub, t, "perfect")
    row = grade_root(bad)["questions"][q][0]
    ok(row["quality"] == 0.0 and "inputs_mismatch" in row["failures"], f"R5b a run on other inputs is refused: {row}")
    lone = os.path.join(td, "lone", "runs", "r")
    build_fixture(lone, rub, t, "perfect")
    s = grade_run(q, lone, truth=t)
    ok(s["quality"] == 0.0 and any(f["code"] == "inputs_unrecorded" for f in s["failures"]),
       "R5c a run with no launch record of its inputs is refused unless --unverified-inputs")
    # preflight hashes the selected tree's archived inputs, not the grader checkout's
    tree = os.path.join(td, "tree")
    os.makedirs(os.path.join(tree, "data", "evals", q))
    for n in os.listdir(os.path.join(DATA, q)):
        shutil.copy(os.path.join(DATA, q, n), os.path.join(tree, "data", "evals", q, n))
    git = ["git", "-C", tree, "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", tree], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "x"], check=True)
    ok(not input_problems(tree, [q]), f"R5d a tree with the same inputs passes preflight: {input_problems(tree, [q])}")
    with open(os.path.join(tree, "data", "evals", q, first), "a") as f:
        f.write("x\n")
    subprocess.run(git + ["commit", "-qam", "y"], check=True)
    ok(input_problems(tree, [q]), "R5e a tree whose committed inputs differ from the truth is refused")
    sys.path.insert(0, os.path.join(REPO, "cloud"))
    import inspect
    import run_cluster
    ok("eval_inputs_sha256" in inspect.getsource(run_cluster.launch)
       and "eval_inputs_sha256" in inspect.getsource(run_cluster.relaunch),
       "R5f launch and relaunch record the shipped inputs' hashes per run")




# ---- review round 2 (Codex P1s): each test reproduces the reported scenario ----------

def _review_r6_numeric_leaks(ok, td):
    t = ensure_truth("flu_vaccine")
    vec = t["allocation"]["optimum"]["blocks"].replace(",", ", ")
    ok(shipped_leaks(REPO, extra_texts={"scripts/x.py": f"# expected split {vec}\n"}),
       "R6 an optimum vector written anywhere in shipped text is caught")
    m = ensure_truth("malaria_alloc")["headlines"]["total_cases_averted"]["value_of_optimum"]
    ok(shipped_leaks(REPO, extra_texts={"docs/x.md": f"about {round(m):,} cases\n"}),
       "R6b a headline number from a truth file is caught")
    ok(not [h for h in shipped_leaks(REPO) if "numeric" in h], "R6c no shipped file holds a truth number")


def _review_r7_cloud_only(ok, td):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["run", "--local", "tb_acf"])
    ok(rc != 0 and "cloud" in buf.getvalue().lower(), f"R7 run --local refuses, pointing to the cloud: {rc} {buf.getvalue()!r}")
    q = open(os.path.join(EVALS, "tb_acf", "question.txt")).read()
    clean = os.path.join(td, "clean")
    os.makedirs(os.path.join(clean, "evals"))
    saved = os.environ.pop("MA_EVAL_ISOLATED", None)
    try:
        ok(eval_paths.eval_workspace_problem(q, clean), "R7b a clean local directory is not isolation: refused")
        os.environ["MA_EVAL_ISOLATED"] = "cloud-worker"
        ok(eval_paths.eval_workspace_problem(q, clean) is None, "R7c a cloud worker's clean workspace may run it")
        ok(eval_paths.eval_workspace_problem(q, REPO), "R7d the marker without a clean root is still refused")
    finally:
        os.environ.pop("MA_EVAL_ISOLATED", None)
        if saved is not None:
            os.environ["MA_EVAL_ISOLATED"] = saved
    with open(os.path.join(REPO, "cloud", "worker_side.py")) as f:
        ok('env["MA_EVAL_ISOLATED"] = "cloud-worker"' in f.read(), "R7e the cloud worker sets the isolation marker")


def _review_r8_resume(ok, td):
    q = open(os.path.join(EVALS, "dengue_outbreak", "question.txt")).read()
    rd = os.path.join(td, "run")
    os.makedirs(rd)
    with open(os.path.join(rd, "metadata.json"), "w") as f:
        json.dump({"question": q}, f)
    ok(eval_paths.question_for(None, rd) == (q, {"question": q}) and eval_paths.question_for("given", rd)[0] == "given",
       "R8 the effective question comes from --resume metadata when none is given")
    with open(os.path.join(REPO, "main.py")) as f:
        src = f.read()
    ok("eval_workspace_problem(eval_question" in src and "question_for(args.question, resume_dir)" in src,
       "R8b main.py checks the resolved question (including --resume)")


def _review_r9_detection(ok, td):
    q = open(os.path.join(EVALS, "tb_acf", "question.txt")).read()
    ok(eval_paths.is_eval_question(q), "R9 the full synthetic prefix marks an eval question")
    ok(not eval_paths.is_eval_question("Plan malaria budgets; cite the (evaluation suite) paper."),
       "R9b the marker in the middle of an ordinary question does not")
    ok(eval_paths.is_eval_question("ordinary", {"eval_question": True}), "R9c explicit run metadata marks one")


def _review_r10_rows(ok, td):
    q = "malaria_alloc"
    rub, t = load_rubric(q), ensure_truth(q)
    al = t["allocation"]
    opt = al["optimum"]["choice"]
    for name, extra in (("duplicate", [("D01", "none", 0)]), ("unknown", [("D99", "ITN", 0)])):
        rd = os.path.join(td, name)
        build_fixture(rd, rub, t, "perfect")
        rows = [(u, p, al["benefit"][u][p]) for u, p in opt.items()] + extra
        _write_alloc(rd, ["district", "package", "cases_averted"], rows)
        s = grade_run(q, rd, truth=t, verify_inputs=False)
        cs = [c for c in s["checks"] if c["type"].startswith("allocation_") or c["type"] == "unit_value_accuracy"]
        ok(all(c["score"] == 0 and "allocation_infeasible" in c["failures"] for c in cs),
           f"R10 a {name} unit row makes the plan infeasible: {[(c['id'], c['score'], c['detail']) for c in cs]}")


def _review_r11_launch_id(ok, td):
    q = "tb_acf"
    rub, t = load_rubric(q), ensure_truth(q)
    out = os.path.join(td, q)
    _launch_fixture(out, q, t, {"R1": "L2"})
    _collected(os.path.join(out, "R1"), "L1", rub, t, "perfect")   # collected from an earlier launch
    runs = os.path.join(out, "R1", "runs")
    rd = os.path.join(runs, sorted(os.listdir(runs))[0])
    s = grade_run(q, rd, truth=t)
    ok(s["quality"] == 0.0 and s.get("refused"), f"R11 single-run grading refuses a launch-id mismatch: {s.get('refused')}")
    with open(os.path.join(out, "R1", "result.json"), "w") as f:
        json.dump({"launch_id": "L2"}, f)
    ok(grade_run(q, rd, truth=t)["quality"] == 1.0, "R11b matching launch ids: graded")
