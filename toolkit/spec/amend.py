#!/usr/bin/env python3
"""amend: one path from a finished or blocked v2 run to a re-signable child run (Wave 0, W0-H1).

    python3 -m toolkit.spec.amend <parent_run> --patch <amendment.yaml> [--from-request] [--out DIR]
                                  [--signoff owner|lead]
    python3 -m toolkit.spec.amend --self-test
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (ROOT, os.path.join(ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

RECORD = "amendment_record.json"


class AmendError(ValueError):
    """The amendment is refused; no child run is made."""


def amend(parent, patch=None, from_request=False, out=None, signoff=None, repo=ROOT, log=print) -> dict:
    raise NotImplementedError


def _self_test() -> int:
    import hashlib
    import json
    import shutil
    import tempfile
    import yaml
    from toolkit.spec import schema, signoff as SO
    import compute
    from lib import pipeline_id
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def tree(d):
        h = hashlib.sha256()
        for dp, dirs, fns in os.walk(d):
            dirs.sort()
            for f in sorted(fns):
                p = os.path.join(dp, f)
                h.update(os.path.relpath(p, d).encode() + b"\0" + open(p, "rb").read())
        return h.hexdigest()

    def sha(p):
        return hashlib.sha256(open(p, "rb").read()).hexdigest()

    def refused(fn, m):
        try:
            fn()
            ok(False, m)
        except AmendError as e:
            ok(True, f"{m} ({str(e)[:90]})")
        except Exception as e:  # noqa: BLE001
            ok(False, f"{m}: {type(e).__name__}: {e}")

    def w(root, rel, txt):
        os.makedirs(os.path.dirname(os.path.join(root, rel)) or root, exist_ok=True)
        open(os.path.join(root, rel), "w").write(txt)

    with tempfile.TemporaryDirectory() as td:
        td = os.path.realpath(td)
        par = os.path.join(td, "parent")
        os.makedirs(par)
        w(par, "metadata.json", json.dumps({"pipeline": "v2", "commit": "0" * 40}))
        schema.toy_spec(par)
        SO.write_sheet(par)
        SO.sign(par, "owner")
        psig = SO.signed_sha(par)
        w(par, "run_state.json", json.dumps({"question": "q", "state": "FINAL", "status": "completed_gate_not_accepted",
                                             "signoff": "owner", "max_rounds": 2, "budget_usd": 5.0, "spent_usd": 3.0,
                                             "derived_inputs": True, "history": [{"to": "FINAL"}]}))
        for rel in ("report.md", "critique_domain.yaml", "findings.jsonl", "analysis/manifest.json",
                    "analysis/point.yaml", "figures/a.png", "engine_choice.json"):
            w(par, rel, "stale\n")
        w(par, "data/derived.csv", "u,v\n1,2\n")
        w(par, "analysis/benchmarks_at_spec_values.record.json", "{}\n")
        w(par, "models/fn.py", "def f(p):\n    return {'y': p['x']}\n")
        w(par, "gone.py", "def f(p):\n    return {'y': 2 * p['x']}\n")
        for fn, out in (("models/fn.py::f", "analysis/batch/points.jsonl"), ("gone.py::f", "analysis/b2/points.jsonl")):
            compute.run_map(par, fn, [{"x": i} for i in range(3)], os.path.join(par, out), backend="local",
                            max_workers=1, ledger=False)
        os.remove(os.path.join(par, "gone.py"))
        p_rows = compute.reuse_report(par)
        before = tree(par)
        pf = os.path.join(td, "a1.yaml")
        yaml.safe_dump({"id": "A1", "reason": "raise the envelope", "spec": [
            {"set": "constraints.envelope_usd", "value": 9500},
            {"set": "analysis.decision_tests[id=DT1].text", "value": "Does the plan change?"}]}, open(pf, "w"))
        rec = amend(par, pf, log=lambda *a: None)
        child = rec.get("child", "")
        meta = json.load(open(os.path.join(child, "metadata.json")))
        cst = json.load(open(os.path.join(child, "run_state.json")))
        cspec = yaml.safe_load(open(os.path.join(child, "decision_spec.yaml")))
        ok(tree(par) == before and SO.status(par) == "signed" and SO.status(child) == "awaiting"
           and child == par + ".amend1",
           f"AM_CHILD_KEEPS_PARENT_SIGNED the parent is unchanged and signed; the child awaits a signature ({child})")
        ok(meta.get("parent_run", {}).get("run_dir") == par and meta.get("parent_spec_sha256") == psig
           and meta["parent_run"].get("run_state_sha256") == sha(os.path.join(par, "run_state.json"))
           and meta.get("pipeline") == "v2", f"AM_LINEAGE parent_run and parent_spec_sha256 recorded ({meta})")
        ok(cspec["constraints"]["envelope_usd"] == 9500 and cspec["analysis"]["decision_tests"][0]["text"]
           == "Does the plan change?" and [c["path"] for c in rec["changes"]] == [
               "constraints.envelope_usd", "analysis.decision_tests[id=DT1].text"]
           and rec["changes"][0]["before"] == 9000, f"AM_PATCH_APPLIED set by path and by [id=...] ({rec['changes']})")
        gone = [x for x in ("report.md", "critique_domain.yaml", "findings.jsonl", "analysis/manifest.json",
                            "analysis/point.yaml", "figures", "engine_choice.json", "decision_spec.signoff.json",
                            "decision_brief.signed", "data/geometry_signoff.json") if os.path.lexists(os.path.join(child, x))]
        kept = [x for x in ("data/derived.csv", "analysis/benchmarks_at_spec_values.record.json", "models/fn.py",
                            "analysis/batch/points.jsonl.summary.json", "units.csv")
                if not os.path.exists(os.path.join(child, x))]
        ok(not gone and not kept, f"AM_CHILD_CONTENT run outputs and sign-off records left out ({gone}); inputs and "
                                  f"stored rows kept ({kept})")
        ok(cst.get("state") == "SIGN" and cst.get("paused") == "awaiting_signoff" and cst.get("signoff") == "owner"
           and cst.get("spent_usd") == 0 and cst.get("budget_usd") == 5.0 and cst.get("max_rounds") == 2,
           f"AM_STATE the child waits at SIGN for the owner ({cst})")
        src = SO.sheet_sources(open(os.path.join(child, SO.SHEET)).read()) or {}
        ssha, shsha = sha(os.path.join(child, "decision_spec.yaml")), sha(os.path.join(child, SO.SHEET))
        cmd = rec.get("commands", {}).get("owner_sign", "")
        ok(src.get("spec_sha256") == ssha and rec.get("dry_sign", {}).get("ok") is True
           and f"--expect-sha256 {ssha}" in cmd and f"--expect-sheet-sha256 {shsha}" in cmd
           and SO.status(child) == "awaiting",
           f"AM_SHEET_MATCHES_SPEC the sheet shows the child spec; a copy dry-signs; the child stays unsigned ({cmd})")
        r = rec.get("reuse", {})
        bys = {b["out"]: b for b in r.get("batches", [])}
        ok(r.get("stored") == 6 and r.get("reusable") == 3 and bys.get("analysis/batch/points.jsonl", {}).get(
            "to_compute") == 0 and bys.get("analysis/b2/points.jsonl", {}).get("to_compute") == 3
           and "not found" in bys.get("analysis/b2/points.jsonl", {}).get("why", "")
           and p_rows["eligible"] == 3, f"AM_REUSE_REPORT_COUNTS per batch, rows reused and to compute, with why ({r})")
        pin = meta.get("code_pin") or {}
        ok(pin.get("commit") == pipeline_id.code_commit(ROOT) and "clean" in pin and "on_remote" in pin
           and rec.get("code_pin") == pin, f"AM_CODE_PIN_RECORDED ({pin})")
        launch = rec.get("commands", {}).get("launch", "")
        smoke = rec.get("commands", {}).get("smoke", "")
        ok("main.py --v2 --resume" in launch and "--signoff owner" in launch and "metadata.json:parent_run" in smoke
           and "metadata.json:code_pin" in smoke and "amendment_record.json:reuse" in smoke
           and os.path.isfile(os.path.join(child, RECORD)), f"AM_COMMANDS launch and smoke markers ({launch})")
        try:
            SO.sign(child, "owner", expect_sha256=ssha, expect_sheet_sha256=shsha,
                    expect_derivations_sha256=rec.get("derivations_sha256"))
            ok(SO.status(child) == "signed", "AM_OWNER_COMMAND_SIGNS the hashes printed sign the child")
        except ValueError as e:
            ok(False, f"AM_OWNER_COMMAND_SIGNS the hashes printed sign the child: {e}")
        # --from-request: the S7 pause's request, with a patch, names the parent's signed spec
        rq = {"round": 1, "signed_spec_sha256": psig, "briefs": [{"task": "T1", "findings": ["D-1"], "brief": "b"}],
              "findings": [{"id": "D-1", "severity": "HIGH", "claim": "c"}],
              "patch": {"reason": "from the request", "spec": [{"set": "constraints.envelope_usd", "value": 9100}]}}
        w(par, "spec_rethink_request.json", json.dumps(rq))
        rec2 = amend(par, from_request=True, signoff="lead", log=lambda *a: None)
        c2 = rec2.get("child", "")
        ok(c2 == par + ".amend2" and yaml.safe_load(open(os.path.join(c2, "decision_spec.yaml")))["constraints"][
            "envelope_usd"] == 9100 and rec2.get("request", {}).get("briefs") == rq["briefs"]
           and json.load(open(os.path.join(c2, "run_state.json")))["signoff"] == "lead"
           and rec2.get("dry_sign", {}).get("by") == "lead" and rec2["dry_sign"].get("ok") is True,
           f"AM_FROM_RETHINK_REQUEST the request's patch and why; a lead child dry-signs as the lead ({c2})")
        w(par, "spec_rethink_request.json", json.dumps(dict(rq, signed_spec_sha256="f" * 64)))
        refused(lambda: amend(par, from_request=True, log=lambda *a: None),
                "AM_STALE_REQUEST_REFUSED a request made for another signed spec")
        w(par, "spec_rethink_request.json", json.dumps({k: v for k, v in rq.items() if k != "patch"}))
        refused(lambda: amend(par, from_request=True, log=lambda *a: None),
                "AM_REQUEST_WITHOUT_PATCH_REFUSED a request with no patch and no --patch")
        os.remove(os.path.join(par, "spec_rethink_request.json"))
        n_dirs = sorted(os.listdir(td))
        bad = {"unresolved": {"reason": "r", "spec": [{"set": "constraints.nope", "value": 1}]},
               "two ops": {"reason": "r", "spec": [{"set": "constraints.envelope_usd", "remove": "x", "value": 1}]},
               "no reason": {"spec": [{"set": "constraints.envelope_usd", "value": 1}]},
               "no-op": {"reason": "r", "spec": [{"set": "constraints.envelope_usd", "value": 9000}]},
               "add existing": {"reason": "r", "spec": [{"add": "constraints.envelope_usd", "value": 1}]},
               "ambiguous selector": {"reason": "r", "spec": [{"remove": "analysis.decision_tests[id=DT9]"}]},
               "invalid spec": {"reason": "r", "spec": [{"set": "comparator.package", "value": "nope"}]},
               "not a mapping": ["x"]}
        for name, p in bad.items():
            yaml.safe_dump(p, open(pf, "w"))
            refused(lambda: amend(par, pf, log=lambda *a: None), f"AM_BAD_PATCH_REFUSED {name}")
        ok(sorted(os.listdir(td)) == n_dirs and tree(par) == before,
           "AM_BAD_PATCH_REFUSED leaves no child and the parent unchanged")
        yaml.safe_dump({"reason": "r", "spec": [{"set": "constraints.envelope_usd", "value": 9200}]}, open(pf, "w"))
        st = json.load(open(os.path.join(par, "run_state.json")))
        w(par, "run_state.json", json.dumps(dict(st, state="ANALYZE", status=None, paused=None)))
        refused(lambda: amend(par, pf, log=lambda *a: None), "AM_RUNNING_PARENT_REFUSED a parent neither final nor paused")
        w(par, "run_state.json", json.dumps(st))
        os.symlink(os.path.join(td, "a1.yaml"), os.path.join(par, "data", "link.yaml"))
        refused(lambda: amend(par, pf, log=lambda *a: None), "AM_LINK_REFUSED a parent holding a link")
        os.remove(os.path.join(par, "data", "link.yaml"))
        shutil.rmtree(c2)
    if fails:
        print(f"FAIL: {len(fails)} self-test case(s) failed")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    return 2


if __name__ == "__main__":
    sys.exit(main())
