#!/usr/bin/env python3
"""The EVIDENCE stage's harness side (v2 autonomy 1B-ii, owner default D1): the evidence dossier drafted in the run.

    python3 toolkit/spec/evidence_draft.py --self-test

The owner's sign-off (toolkit/spec/signoff.py) refuses a spec whose options need decision inputs unless every one has
a checked, sourced entry in the evidence dossier (toolkit/spec/evidence_dossier.py). Before this stage no task could
write that file, so a run under `--signoff lead` waited at SIGN for the owner. The stage machine's EVIDENCE state now
runs before SIGN:

1. assess(): what the spec's options need (evidence_dossier.required_inputs, the check the signer runs), whether the
   spec names the dossier, and why the dossier does not yet cover it (evidence_dossier.check).
2. scaffold(): for each required input with no entry, an entry with what execution itself uses (model_value,
   model_source, and the card default as `chosen`, with the generated range test for a drawn parameter), computed by
   the dossier module's own execution view. The harness never writes observations or `no_data`: the evidence is the
   task's, and an entry without it stays refused.
3. The bounded draft_evidence task (agents/v2_sdk_tasks.py) fills the evidence, at most MAX_TRIES times, each time
   with the check's problems.
4. finish(): evidence/dossier_draft.json, the record the harness checks and the report renders: the inputs required,
   the problems left, and the declared gaps (no data, a chosen value that differs from what execution uses, a value
   left at the card default, a source the harness could not locate).

Nothing here loosens the check: signing still runs evidence_dossier's full precondition on the files. Nothing names a
disease; the inputs come from the engine's card.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (ROOT, os.path.join(ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from toolkit.spec import evidence_dossier as ED  # noqa: E402

RECORD = "evidence/dossier_draft.json"
SCAFFOLD = "harness scaffold"          # the prefix of every text the scaffold writes
MAX_TRIES = 3                          # one draft and two repairs with the check's problems (D1)
SPEC = "decision_spec.yaml"


def _spec(run_dir: str) -> dict:
    raise NotImplementedError("v2 autonomy 1B-ii β")


def _doc(data: bytes | None) -> dict:
    """The dossier's `inputs` mapping, or {} for an absent or malformed file (the check names the fault)."""
    raise NotImplementedError("v2 autonomy 1B-ii β")


def assess(run_dir: str, raw: dict | None = None) -> dict:
    """{needed, rel, required, spec_problems, problems}: spec_problems go back to the spec draft (a component the
    engines cannot resolve, or a spec that needs a dossier and names none); problems are the dossier's own (coverage
    and validity, the signer's check). Reads the files every call."""
    raise NotImplementedError("v2 autonomy 1B-ii β")


def packet(a: dict) -> dict:
    """The draft_evidence task's input: each required input (kind, name, why, used by, the engines and their
    directories, whose card, priors and declarations it reads) and the dossier's path."""
    raise NotImplementedError("v2 autonomy 1B-ii β")


def _range(x) -> bool:
    raise NotImplementedError("v2 autonomy 1B-ii β")


def scaffold_entry(r: dict, raw: dict, run_dir: str, memo: dict) -> dict:
    """What execution uses for one required input, as a dossier entry without its evidence: model_value and
    model_source, and the card default as `chosen` (a range only with a test the runner generates for it, or one the
    run calibrates). Empty when execution's value cannot be computed: the task writes the whole entry."""
    raise NotImplementedError("v2 autonomy 1B-ii β")


def scaffold(run_dir: str, a: dict, raw: dict | None = None) -> dict:
    """Add a scaffolded entry for every required input the dossier lacks; entries already there (the task's or the
    owner's) are kept as they are. Writes the dossier (atomic, no link followed) only when an entry was added.
    Returns {"added": [keys], "scaffold": {key: entry}} (the scaffold of every required input, for finish())."""
    raise NotImplementedError("v2 autonomy 1B-ii β")


def _where_found(where, run_dir: str) -> str:
    """found | url | unchecked: whether an observation's `where` names a file the harness can see (run-relative, or in
    the repository), a web address it does not open, or anything else (a table name, a citation)."""
    raise NotImplementedError("v2 autonomy 1B-ii β")


def gaps(raw: dict, run_dir: str, data: bytes | None, req: dict, sc: dict) -> dict:
    """The declared gaps: {no_data, differs, default_kept, sources_unchecked}, each a sorted list of input keys."""
    raise NotImplementedError("v2 autonomy 1B-ii β")


def finish(run_dir: str, a: dict, sc: dict, tries: int, raw: dict | None = None, notes: list | None = None) -> dict:
    """Write evidence/dossier_draft.json from the files as they are now and return it."""
    raise NotImplementedError("v2 autonomy 1B-ii β")


def read_record(run_dir: str) -> dict | None:
    raise NotImplementedError("v2 autonomy 1B-ii β")


GAP_TEXT = (("no_data", "No field or trial data; a value borrowed by the entry's stated rule"),
            ("differs", "The dossier's chosen value or range differs from what execution used (the run used "
                        "execution's; the owner decides)"),
            ("default_kept", "Left at the engine's value by the harness scaffold (the task did not change it)"),
            ("sources_unchecked", "Cited sources the harness could not locate as files (web addresses or citations, "
                                  "not opened)"))


def report_block(run_dir: str, signed_by: str | None) -> list[str]:
    """The report's evidence section, from the record: who drafted and who signed the dossier and every declared gap.
    Empty when the run needed no dossier."""
    raise NotImplementedError("v2 autonomy 1B-ii β")


# ----------------------------------------------------------------------------------------------- self-test
def _self_test() -> int:
    import copy
    import tempfile
    import yaml
    from toolkit.spec import toy_engines as T
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    T.enable_for_test()
    with tempfile.TemporaryDirectory() as td:
        # EDR1: the spec names no dossier although its options need inputs: a spec problem, nothing written
        d = os.path.join(td, "nodossier")
        os.makedirs(d)
        ED.toy_dossier_spec(d, dossier=False)
        a = assess(d)
        ok(a["needed"] and a["spec_problems"] and "name the evidence dossier" in a["spec_problems"][-1]
           and not os.path.exists(os.path.join(d, ED.DOSSIER_FILE)),
           f"EDR1 a spec that needs inputs and names no dossier goes back to the draft ({a['spec_problems'][-1:]})")
        # EDR2: no inputs needed, no dossier named: nothing to do, a zero-cost pass
        d = os.path.join(td, "none")
        os.makedirs(d)
        ED.toy_dossier_spec(d, ref="toolkit.engines.contract:ToyEngine", dossier=False)
        a = assess(d)
        rec = finish(d, a, {}, 0)
        ok(not a["needed"] and not a["spec_problems"] and rec["status"] == "not_required"
           and report_block(d, "lead") == [], f"EDR2 no decision inputs: not required, no report block ({rec})")
        # EDR3: the scaffold fills what execution uses and never the evidence; the check still refuses it
        d = os.path.join(td, "scaffold")
        os.makedirs(d)
        ED.toy_dossier_spec(d)
        os.remove(os.path.join(d, ED.DOSSIER_FILE))
        a = assess(d)
        s = scaffold(d, a)
        ins = _doc(open(os.path.join(d, ED.DOSSIER_FILE), "rb").read())
        ea, ek, ex = ins.get("parameter:eff_a", {}), ins.get("parameter:k", {}), ins.get("unit_attribute:x", {})
        ok(s["added"] == ["parameter:eff_a", "parameter:k", "unit_attribute:x"]
           and ea.get("model_value") == [0.3, 0.7] and ea.get("decision_test") == "fitted_range_eff_a"
           and ea["chosen"]["range"] == [0.3, 0.7] and ek.get("chosen", {}).get("value") == 0.8
           and ex.get("chosen", {}).get("range") == [-2, 2] and "decision_test" not in ex
           and all("observations" not in e and "no_data" not in e for e in ins.values())
           and all(e["model_source"].startswith(SCAFFOLD) for e in ins.values()),
           f"EDR3 the scaffold writes execution's values and the card default, never observations or no_data: {ins}")
        a = assess(d)
        ok(a["problems"] and all("exactly one of observations and no_data" in x for x in a["problems"])
           and len(a["problems"]) == 3, f"EDR4 a scaffold alone does not pass the signer's check: {a['problems']}")
        # EDR5: the task's entries are kept; a missing one is scaffolded again; a broken file is left to the task
        full = copy.deepcopy(ED.toy_entries())
        full.pop("parameter:k")
        yaml.safe_dump({"inputs": full}, open(os.path.join(d, ED.DOSSIER_FILE), "w"), sort_keys=False)
        s = scaffold(d, assess(d))
        ins = _doc(open(os.path.join(d, ED.DOSSIER_FILE), "rb").read())
        ok(s["added"] == ["parameter:k"] and ins["parameter:eff_a"] == ED.toy_entries()["parameter:eff_a"],
           f"EDR5 entries already written are kept; only the missing input is scaffolded ({s['added']})")
        open(os.path.join(d, ED.DOSSIER_FILE), "w").write("inputs: [unclosed")
        s = scaffold(d, assess(d))
        ok(s["added"] == [] and open(os.path.join(d, ED.DOSSIER_FILE)).read() == "inputs: [unclosed",
           "EDR6 a dossier that does not parse is not overwritten: the task repairs it with the check's error")
        # EDR7: a complete dossier: the record's gaps declare no data, a kept default, a moved value, a source
        e = copy.deepcopy(ED.toy_entries())
        e["parameter:k"] = dict(s["scaffold"]["parameter:k"], no_data={"reason": "no field data",
                                                                      "borrowing_rule": "the card's value"})
        e["parameter:eff_a"]["chosen"] = {"range": [0.3, 0.6], "rule": "the trial's interval"}
        e["parameter:eff_a"]["decision_test"] = "DT_eff_a"
        e["parameter:eff_a"]["observations"][0]["where"] = "data/inputs/trial.csv: Table 2"
        os.makedirs(os.path.join(d, "data", "inputs"))
        open(os.path.join(d, "data", "inputs", "trial.csv"), "w").write("a\n")
        e["unit_attribute:x"]["observations"] = [dict(ED.toy_entries()["parameter:eff_a"]["observations"][0],
                                                      where="https://example.org/survey")]
        e["unit_attribute:x"].pop("no_data")
        e["unit_attribute:x"]["comparison"] = "the survey's value is inside the calibration's range"
        yaml.safe_dump({"inputs": e}, open(os.path.join(d, ED.DOSSIER_FILE), "w"), sort_keys=False)
        a = assess(d)
        rec = finish(d, a, s["scaffold"], 2)
        g = rec.get("gaps") or {}
        ok(rec["status"] == "complete" and a["problems"] == [] and g.get("no_data") == ["parameter:k"]
           and g.get("default_kept") == ["parameter:k"] and g.get("differs") == ["parameter:eff_a"]
           and g.get("sources_unchecked") == ["unit_attribute:x"] and rec["drafted_by_task"],
           f"EDR7 the record declares each gap by input: {rec}")
        blk = "\n".join(report_block(d, "lead"))
        ok("Provisional" in blk and "parameter:k" in blk and "parameter:eff_a" in blk and "unit_attribute:x" in blk
           and "drafted by a run task" in blk and "Provisional" not in "\n".join(report_block(d, "owner")),
           f"EDR8 the report states who drafted and signed the dossier and every gap; provisional unless the owner "
           f"signed:\n{blk}")
        ok(json.load(open(os.path.join(d, RECORD)))["dossier_sha256"] == ED.sha(
            open(os.path.join(d, ED.DOSSIER_FILE), "rb").read()), "EDR9 the record binds the dossier it describes")
        keep = open(os.path.join(d, ED.DOSSIER_FILE), "rb").read()
        open(os.path.join(d, ED.DOSSIER_FILE), "ab").write(b"# edited by the owner\n")
        blk2 = "\n".join(report_block(d, "owner"))
        ok("changed after the evidence stage" in blk2 and "parameter:k" not in blk2,
           f"EDR9b a dossier edited after the stage: the record's gaps are not reported as its gaps:\n{blk2}")
        open(os.path.join(d, ED.DOSSIER_FILE), "wb").write(keep)
        # EDR10: fail closed on an unreadable record; a link in place of the dossier is refused, never followed
        open(os.path.join(d, RECORD), "w").write("{not json")
        ok(read_record(d) == {"status": "unreadable"} and "could not be read" in "\n".join(report_block(d, "lead")),
           "EDR10 an unreadable record is reported as such")
        tgt = os.path.join(td, "elsewhere.yaml")
        open(tgt, "w").write("inputs: {}\n")
        os.remove(os.path.join(d, ED.DOSSIER_FILE))
        os.symlink(tgt, os.path.join(d, ED.DOSSIER_FILE))
        try:
            scaffold(d, assess(d))
            refused = False
        except ValueError:
            refused = True
        ok(refused and open(tgt).read() == "inputs: {}\n", "EDR11 a dossier path that is a link is refused")
        # EDR12: a spec naming another file as its dossier (one another task owns): the scaffold never writes it
        d = os.path.join(td, "other")
        os.makedirs(d)
        p = ED.toy_dossier_spec(d, dossier=False)
        raw = yaml.safe_load(open(p))
        raw["evidence"] = {"dossier": "analysis_settings.yaml"}
        yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
        open(os.path.join(d, "analysis_settings.yaml"), "w").write("uncertainty: {draws: 8}\n")
        a = assess(d)
        s = scaffold(d, a)
        ok(not a["draftable"] and s["added"] == [] and a["problems"]
           and open(os.path.join(d, "analysis_settings.yaml")).read() == "uncertainty: {draws: 8}\n",
           f"EDR12 only {ED.DOSSIER_FILE} is drafted: a spec naming another file has nothing written to it")
        ok(packet(a)["required"]["parameter:eff_a"]["engines"] and packet(a)["dossier"] == "analysis_settings.yaml",
           "EDR13 the task's packet lists each input with its engines")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
