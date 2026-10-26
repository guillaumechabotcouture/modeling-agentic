#!/usr/bin/env python3
"""
E7 quality scoring: blind the reports, score them with two model judges from
different families, and measure agreement with the owner's scores.

    python3 scripts/score_reports.py prepare --out DIR NAME=RUN_DIR [NAME=RUN_DIR ...]
    python3 scripts/score_reports.py judge DIR [--judges codex,claude] [--dry-run]
    python3 scripts/score_reports.py agree DIR --owner owner_scores.json
    python3 scripts/score_reports.py --self-test

prepare  For each run: a random label (A, B, C, ...). DIR/blind/<L>/report.md
         is the owner's copy (run dates, phase names and harness vocabulary
         neutralised; the final-gate verdict, if any, is kept, as the E7
         amendment requires). DIR/blind/<L>/bundle.md is the judges' common
         evidence bundle: the report plus results.md, decision_rule.md,
         citations.md and small model/data summaries. The label key goes to
         DIR/key.json, which no rater sees.
judge    Each judge scores every label against the E7 rubric (D1-D6, 1-5,
         each with a quoted piece of evidence), in a random order:
         - codex: gpt-6.1-sol through `codex exec -s read-only` on bundle.md;
         - claude: claude-opus-5-5 through the Messages API with bundle.md.
         Output: DIR/scores/<judge>/<L>.json.
agree    Judge-vs-owner agreement per dimension (exact and within +/-1), and
         the dimensions where the judges differ from the owner by > 1 point on
         average (not trusted for automated scoring, E7 rule 4).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import string
import subprocess
import sys
import urllib.request

DIMENSIONS = {
    "D1": ("Answers the question",
           "1: no allocation, or one not tied to the stated budget; 3: allocation present, partly justified; "
           "5: a usable LGA x package allocation within ~$320M, with the trade-offs stated"),
    "D2": ("Decision rule usable",
           "1: none, or a rule restating the table; 3: a rule with gaps or exceptions left unexplained; "
           "5: a short rule a program officer could apply, with its accuracy vs the optimizer stated"),
    "D3": ("Numbers traceable",
           "1: headline numbers not found in any artifact; 3: most traceable, some contradict their source; "
           "5: every headline number traceable to an artifact; no contradictions"),
    "D4": ("Uncertainty honest",
           "1: point estimates only, or degenerate CIs; 3: CIs present but incomplete, or sensitivity not "
           "reflected in the conclusions; 5: CIs on the headline outputs; conclusions qualified where "
           "sensitivity shows instability"),
    "D5": ("External plausibility",
           "1: headline totals implausible (e.g. DALYs averted above the national burden; nets above the supply "
           "ceiling); 3: plausible but unchecked; 5: totals checked against named external benchmarks "
           "(national burden, net supply, the GC7 envelope)"),
    "D6": ("Limitations stated",
           "1: absent or boilerplate; 3: present but missing a load-bearing one; 5: the load-bearing "
           "limitations are named, with their likely effect on the decision"),
}
BUNDLE_FILES = ["results.md", "decision_rule.md", "citations.md"]
BUNDLE_GLOBS_MAX_KB = 60          # per extra summary file
EXTRA_SUMMARIES = ["models/claims_ledger.yaml", "uncertainty_report.yaml", "models/sensitivity_analysis.yaml",
                   "benchmark_match.yaml", "final_gate.json"]
# Identifying the code version for the owner's copy (E7 blinding).
_NEUTRALISE = [
    (re.compile(r"\b20\d\d-\d\d-\d\d(?:[T _]\d\d[:\d]*)?\b"), "[date]"),
    (re.compile(r"\bPhase\s+\d+\s*[α-ω′″‴⁗]*", re.I), "[process]"),
    (re.compile(r"\[CLAIM:[^\]]+\]"), "[value]"),
    (re.compile(r"\b(STAGE\s+\d+|validate_critique_yaml|effort[_ ]floors?|claims[_ ]ledger|rigor[_ ]artifacts?|"
                r"critique_[a-z]+\.ya?ml|harness)\b", re.I), "[process]"),
]


def neutralise(text: str) -> str:
    for pat, rep in _NEUTRALISE:
        text = pat.sub(rep, text)
    return text


def _read(p, limit_kb=None):
    try:
        data = open(p, errors="replace").read()
    except OSError:
        return None
    if limit_kb and len(data) > limit_kb * 1024:
        data = data[: limit_kb * 1024] + "\n[... truncated ...]\n"
    return data


def build_bundle(run_dir: str) -> str:
    parts = ["# Evidence bundle", "", "## report.md", "", _read(os.path.join(run_dir, "report.md")) or "(missing)"]
    for f in BUNDLE_FILES + EXTRA_SUMMARIES:
        body = _read(os.path.join(run_dir, f), None if f in BUNDLE_FILES else BUNDLE_GLOBS_MAX_KB)
        if body:
            parts += ["", f"## {f}", "", body]
    return "\n".join(parts)


def prepare(out: str, runs: dict[str, str], seed: int | None = None) -> dict:
    rng = random.Random(seed)
    names = list(runs)
    rng.shuffle(names)
    key = {}
    for label, name in zip(string.ascii_uppercase, names):
        rd = runs[name]
        d = os.path.join(out, "blind", label)
        os.makedirs(d, exist_ok=True)
        report = _read(os.path.join(rd, "report.md"))
        if report is None:
            raise SystemExit(f"{name}: no report.md in {rd}")
        open(os.path.join(d, "report.md"), "w").write(neutralise(report))
        open(os.path.join(d, "bundle.md"), "w").write(neutralise(build_bundle(rd)))
        key[label] = {"name": name, "run_dir": os.path.abspath(rd)}
    json.dump(key, open(os.path.join(out, "key.json"), "w"), indent=1)
    return key


def rubric_prompt(question: str | None = None) -> str:
    dims = "\n".join(f"- {k} {v[0]}: {v[1]}" for k, v in DIMENSIONS.items())
    return f"""You are an independent expert reviewer scoring one modeling report for a public-health budget
decision. The report and its supporting evidence are in bundle.md. Score each dimension 1-5 using the anchors
(2 and 4 are between anchors). Every score MUST quote the exact sentence or number from bundle.md that justifies
it. Judge only what is in the bundle; do not reward length or tone.

Question the report answers: Nigeria malaria allocation across 774 LGAs for the Global Fund GC7 request
(~$320M), with ITNs/LLINs, IRS, SMC and case management.

Dimensions:
{dims}

Reply with ONLY a JSON object:
{{"scores": {{"D1": {{"score": <1-5>, "evidence": "<quote>"}}, ... "D6": {{...}}}},
  "overall_note": "<two sentences>"}}"""


def _parse_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        j = json.loads(m.group(0))
    except ValueError:
        return None
    s = j.get("scores") or {}
    if not all(isinstance(s.get(k), dict) and s[k].get("score") in (1, 2, 3, 4, 5) for k in DIMENSIONS):
        return None
    return j


def judge_codex(label_dir: str) -> dict | None:
    out = os.path.join(label_dir, ".codex_out.txt")
    subprocess.run(["codex", "exec", "-m", "gpt-6.1-sol", "-s", "read-only", "-o", out,
                    rubric_prompt() + "\n\nRead bundle.md in the current directory."],
                   cwd=label_dir, capture_output=True, text=True, timeout=3600)
    return _parse_json(_read(out) or "")


def judge_claude(label_dir: str, model: str = "claude-opus-5-5") -> dict | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY not set")
    bundle = _read(os.path.join(label_dir, "bundle.md"))
    body = json.dumps({"model": model, "max_tokens": 4000,
                       "messages": [{"role": "user", "content": rubric_prompt() + "\n\n<bundle.md>\n"
                                     + bundle + "\n</bundle.md>"}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        resp = json.loads(r.read())
    text = "".join(b.get("text", "") for b in resp.get("content") or [] if b.get("type") == "text")
    j = _parse_json(text)
    if j is not None:
        j["usage"] = resp.get("usage")
    return j


JUDGES = {"codex": judge_codex, "claude": judge_claude}


def judge(out: str, judges: list[str], dry_run: bool = False, seed: int | None = None) -> dict:
    labels = sorted(os.listdir(os.path.join(out, "blind")))
    results = {}
    for j in judges:
        order = labels[:]
        random.Random(seed).shuffle(order)             # each judge sees the reports in a random order
        os.makedirs(os.path.join(out, "scores", j), exist_ok=True)
        for L in order:
            d = os.path.join(out, "blind", L)
            if dry_run:
                res = {"dry_run": True, "prompt_chars": len(rubric_prompt()),
                       "bundle_chars": len(_read(os.path.join(d, "bundle.md")) or "")}
            else:
                res = JUDGES[j](d) or {"error": "no valid JSON scores returned"}
            json.dump(res, open(os.path.join(out, "scores", j, f"{L}.json"), "w"), indent=1)
            results.setdefault(j, {})[L] = res
    return results


def agree(out: str, owner: dict) -> dict:
    """owner: {label: {D1: int, ...}}. Per judge and dimension: exact and +/-1
    agreement, mean absolute difference; dimensions with mean |diff| > 1 flagged."""
    rep = {}
    sd = os.path.join(out, "scores")
    for j in sorted(os.listdir(sd)):
        per = {}
        for D in DIMENSIONS:
            diffs = []
            for L, o in owner.items():
                try:
                    s = json.load(open(os.path.join(sd, j, f"{L}.json")))["scores"][D]["score"]
                except (OSError, KeyError, ValueError, TypeError):
                    continue
                diffs.append(s - o[D])
            if diffs:
                per[D] = {"n": len(diffs), "exact": sum(d == 0 for d in diffs) / len(diffs),
                          "within1": sum(abs(d) <= 1 for d in diffs) / len(diffs),
                          "mean_abs": sum(abs(d) for d in diffs) / len(diffs),
                          "not_trusted": sum(abs(d) for d in diffs) / len(diffs) > 1}
        rep[j] = per
    json.dump(rep, open(os.path.join(out, "agreement.json"), "w"), indent=1)
    return rep


def _self_test() -> int:
    import tempfile
    fails = []
    with tempfile.TemporaryDirectory() as t:
        runs = {}
        for name, body in (("new", "# R\nRun 2026-10-26 under Phase 29: STAGE 7 [CLAIM:dalys] averted.\n"),
                           ("old", "# R\nOld report.\n")):
            rd = os.path.join(t, name); os.makedirs(os.path.join(rd, "models"))
            open(os.path.join(rd, "report.md"), "w").write(body)
            open(os.path.join(rd, "results.md"), "w").write("results\n")
            runs[name] = rd
        out = os.path.join(t, "score")
        key = prepare(out, runs, seed=1)
        if sorted(key) != ["A", "B"] or {v["name"] for v in key.values()} != {"new", "old"}:
            fails.append(f"S1 random labels and a private key: {key}")
        lab = next(L for L, v in key.items() if v["name"] == "new")
        owner_copy = open(os.path.join(out, "blind", lab, "report.md")).read()
        if any(x in owner_copy for x in ("2026-10-26", "Phase 29", "STAGE 7", "[CLAIM:")):
            fails.append(f"S2 the owner's copy hides dates, phases and harness vocabulary: {owner_copy!r}")
        if "results" not in open(os.path.join(out, "blind", lab, "bundle.md")).read():
            fails.append("S3 the judges' bundle includes the supporting evidence")
        res = judge(out, ["codex", "claude"], dry_run=True, seed=1)
        if set(res) != {"codex", "claude"} or not all(len(v) == 2 for v in res.values()):
            fails.append(f"S4 dry run: every judge, every label: {res}")
        good = {"scores": {k: {"score": 3, "evidence": "q"} for k in DIMENSIONS}}
        if _parse_json("noise " + json.dumps(good) + " tail") is None or _parse_json('{"scores": {}}') is not None:
            fails.append("S5 judge output must be a complete, valid score set")
        for L in key:
            json.dump(dict(good, scores={k: {"score": 4 if k == "D1" else 3} for k in DIMENSIONS}),
                      open(os.path.join(out, "scores", "codex", f"{L}.json"), "w"))
        owner = {L: {k: (1 if k == "D1" else 3) for k in DIMENSIONS} for L in key}
        rep = agree(out, owner)
        if not (rep["codex"]["D1"]["not_trusted"] and rep["codex"]["D2"]["exact"] == 1.0):
            fails.append(f"S6 agreement: a 3-point gap is not trusted; equal scores agree: {rep.get('codex')}")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="E7 blind scoring")
    ap.add_argument("cmd", nargs="?", choices=["prepare", "judge", "agree"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--out")
    ap.add_argument("--judges", default="codex,claude")
    ap.add_argument("--owner")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if a.cmd == "prepare":
        runs = dict(x.split("=", 1) for x in a.args)
        key = prepare(a.out, runs, a.seed)
        print(json.dumps({"labels": sorted(key), "out": a.out, "key": os.path.join(a.out, "key.json")}))
        return 0
    if a.cmd == "judge":
        res = judge(a.args[0], a.judges.split(","), a.dry_run, a.seed)
        print(json.dumps({j: {L: ("ok" if "scores" in r or r.get("dry_run") else r.get("error")) for L, r in v.items()}
                          for j, v in res.items()}, indent=1))
        return 0
    if a.cmd == "agree":
        print(json.dumps(agree(a.args[0], json.load(open(a.owner))), indent=1))
        return 0
    ap.error("see --help")
    return 2


if __name__ == "__main__":
    sys.exit(main())
