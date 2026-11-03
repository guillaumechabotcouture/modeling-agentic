#!/usr/bin/env python3
"""Owner sign-off of the decision spec: a thin adapter over scripts/decision_brief.py.

    python3 toolkit/spec/signoff.py status <run_dir>
    python3 toolkit/spec/signoff.py sheet <run_dir>          # what signing accepts beyond the spec's text
    python3 toolkit/spec/signoff.py sign <run_dir> --by owner|lead [--note TEXT]
    python3 toolkit/spec/signoff.py --self-test

`decision_spec.yaml` is one of decision_brief.FROZEN_AT_SIGNOFF: when the run has a brief,
signing the brief freezes the spec with it (same record, same hashes, same restore and
`acceptance_criteria_changed` handling). A v2 run with a spec and no brief gets the same
record for the spec alone (`decision_spec.signoff.json`), until the state machine merges
the two. Either way the spec must validate before it can be signed.

status: absent | invalid | awaiting | changed | signed.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (ROOT, os.path.join(ROOT, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import decision_brief as DB  # noqa: E402
from toolkit.spec import schema  # noqa: E402

SPEC = DB.DECISION_SPEC
SPEC_SIGNOFF = "decision_spec.signoff.json"
# docs/design/v2-derived-inputs.md: the harness's record of the derivations that made the spec's data. The sign-off
# freezes its hash next to the spec's, and the derived files it lists, so a later change to either breaks it.
DERIVATIONS_RECORD = "data/derivations_record.json"
SHEET = "decision_spec.signoff_sheet.md"


def _spec_record(run_dir: str) -> dict | None:
    """The sign-off that covers the spec: the brief's when it hashed the spec, else the spec-only record."""
    so = DB._signoff(run_dir)
    if so is not None and (so.get("sha256") or {}).get(SPEC) is not None:
        return so
    try:
        with open(os.path.join(run_dir, SPEC_SIGNOFF), encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def status(run_dir: str) -> str:
    path = os.path.join(run_dir, SPEC)
    rec = _spec_record(run_dir)
    if not os.path.exists(path):
        return "changed" if rec is not None else "absent"
    if rec is not None:
        return "signed" if DB._sha(path) == (rec.get("sha256") or {}).get(SPEC) and not derived_changed(run_dir, rec) \
            else "changed"
    return "invalid" if schema.validate(schema.load(path), pending_ok=True) else "awaiting"


def derived_changed(run_dir: str, rec: dict) -> list[str]:
    """What changed since sign-off among the derivation record and the derived files it lists (empty: nothing). A
    sign-off made before the record existed, in a run that has none, is unaffected."""
    signed = (rec.get("sha256") or {}).get(DERIVATIONS_RECORD)
    now = DB._sha(os.path.join(run_dir, DERIVATIONS_RECORD))
    if signed is None and now is None:
        return []
    if signed != now:
        return [DERIVATIONS_RECORD]
    try:
        derivs = (json.load(open(os.path.join(run_dir, DERIVATIONS_RECORD))) or {}).get("derivations") or {}
    except (OSError, ValueError):
        return [DERIVATIONS_RECORD]
    return sorted(o for e in derivs.values() for o, h in ((e or {}).get("outputs") or {}).items()
                  if DB._sha(os.path.join(run_dir, o)) != h)


def _freeze_record(run_dir: str, path: str) -> dict:
    """Add the derivation record's hash to a sign-off file (the brief's or the spec's own)."""
    with open(path, encoding="utf-8") as f:
        rec = json.load(f)
    rec.setdefault("sha256", {})[DERIVATIONS_RECORD] = DB._sha(os.path.join(run_dir, DERIVATIONS_RECORD))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=1)
    return rec


def signed_sha(run_dir: str) -> str | None:
    rec = _spec_record(run_dir)
    return (rec.get("sha256") or {}).get(SPEC) if rec else None


def sign(run_dir: str, by: str, note: str = "", answers: dict | None = None, accept_proposed: bool = False) -> dict:
    path = os.path.join(run_dir, SPEC)
    if not os.path.exists(path):
        raise ValueError(f"{SPEC} does not exist")
    errs = schema.validate(schema.load(path), pending_ok=True)
    if errs:
        raise ValueError("spec not ready: " + "; ".join(errs[:10]))
    if _spec_record(run_dir) is not None:
        raise ValueError("the spec is already signed; a change needs a new sign-off by the owner")
    if os.path.exists(os.path.join(run_dir, DB.BRIEF)):
        DB.sign(run_dir, by, answers=answers, note=note, accept_proposed=accept_proposed)
        return _freeze_record(run_dir, os.path.join(run_dir, DB.SIGNOFF))
    record = {"signed_by": by, "signed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(), "note": note,
              "sha256": {SPEC: DB._sha(path), DERIVATIONS_RECORD: DB._sha(os.path.join(run_dir, DERIVATIONS_RECORD))},
              "producer": "toolkit/spec/signoff.py",
              "schema_version": schema.SCHEMA_VERSION}
    os.makedirs(os.path.join(run_dir, DB.SIGNED_DIR), exist_ok=True)
    shutil.copyfile(path, os.path.join(run_dir, DB.SIGNED_DIR, SPEC))
    with open(os.path.join(run_dir, SPEC_SIGNOFF), "w", encoding="utf-8") as f:
        json.dump(record, f, indent=1)
    return record


def sheet(run_dir: str, candidates: list | None = None) -> str:
    """The sign-off sheet (markdown): what signing accepts beyond the spec's text. Each engine default the spec
    accepts by name, with the card's meaning and the spec's reason; the staged columns and tables the derivations
    do not use, with their reasons; the derivations; each engine parameter value the spec sets, with the card value
    it replaces and a HIGH note for one outside the card's range; and, when given, the candidate engines and why any
    is ineligible ({ref, eligible, why_not} rows from the pre-sign check)."""
    import yaml
    path = os.path.join(run_dir, SPEC)
    try:
        raw = yaml.safe_load(open(path, encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        return f"# Decision spec sign-off sheet\n\n{SPEC} unreadable: {e}\n"
    out = ["# Decision spec sign-off sheet", "",
           f"Signing accepts `{SPEC}` (sha256 `{(DB._sha(path) or '')[:16]}`) and the derived data recorded in "
           f"`{DERIVATIONS_RECORD}` (sha256 `{(DB._sha(os.path.join(run_dir, DERIVATIONS_RECORD)) or 'none')[:16]}`). "
           "It also accepts what follows.", ""]
    eng = raw.get("engine")
    refs = [eng["ref"]] if isinstance(eng, dict) and eng.get("ref") else \
        (list(eng.get("candidates") or []) if isinstance(eng, dict) else [])
    refs = refs or [r.get("ref") for r in candidates or [] if r.get("ref")]
    binds = {}
    for ref in refs:
        try:
            binds[ref] = schema.binding_for_ref(ref)
        except Exception:  # noqa: BLE001 -- the sheet still shows the spec's reason
            binds[ref] = {}
    acc = ((raw.get("decision_units") or {}).get("defaults_accepted") or {})
    out.append("## Engine defaults accepted by name")
    if not acc:
        out.append("None: every engine unit attribute is bound to the units table, a value or the calibration.")
    for a, why in sorted(acc.items()):
        means = sorted({f"{r['default']!r}: {r['meaning']}" for r in (schema.binding_rule(b, a) for b in binds.values())
                        if isinstance(r, dict)})
        out.append(f"- **{a}**: " + ("; ".join(means) if means else "the chosen engine's default")
                   + f". Reason: {why}")
    out.append("")
    try:
        import pipeline as P
        ders, ign, errs = P.load_derivations(run_dir)
    except Exception as e:  # noqa: BLE001
        ders, ign, errs = [], {}, [f"{type(e).__name__}: {e}"]
    out.append("## Staged inputs and columns not used")
    rows = [f"- `{inp}`: column `{c}`: {why}" for d in ders
            for inp, cs in sorted(((d.get("columns") or {}).get("ignores") or {}).items()) for c, why in sorted(cs.items())]
    rows += [f"- `{inp}` (the whole table): {why}" for inp, why in sorted(ign.items())]
    out += rows or ["None: every column of every staged table is used."]
    out += ["", "## Derivations"] + [f"- **{d['id']}**: `{d['script']}` reads {len(d['inputs'])} file(s), writes "
                                     + ", ".join(f"`{o}`" for o in d["outputs"]) for d in ders]
    out += [f"- declaration problem: {e}" for e in errs] + [""]
    from toolkit.spec import engine_parameters as PV
    out += PV.sheet_lines(raw)          # each spec value, the card value it replaces, and a HIGH note when outside
    if candidates:
        out.append("## Candidate engines (pre-sign check)")
        for r in candidates:
            out.append(f"- `{r.get('ref')}`: " + ("eligible" if r.get("eligible") else "ineligible: "
                                                  + "; ".join(str(w) for w in r.get("why_not") or [])[:1500]))
        out.append("")
    return "\n".join(out)


def _self_test() -> int:
    import tempfile
    import yaml
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d:
        ok(status(d) == "absent", "SG1 no spec: absent")
        schema.toy_spec(d)
        ok(status(d) == "awaiting", "SG2 a valid unsigned spec: awaiting")
        rec = sign(d, "lead", note="test run")
        ok(status(d) == "signed" and rec["sha256"][SPEC] == signed_sha(d), "SG3 signed (spec-only record)")
        with open(os.path.join(d, SPEC), "a") as f:
            f.write("\n# edited\n")
        ok(status(d) == "changed", "SG4 an edit after sign-off: changed")
        try:
            sign(d, "lead")
            ok(False, "SG5 a signed spec cannot be re-signed in place")
        except ValueError:
            ok(True, "SG5 a signed spec cannot be re-signed in place")
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d)
        raw = yaml.safe_load(open(p))
        raw["comparator"]["package"] = "nope"
        yaml.safe_dump(raw, open(p, "w"))
        ok(status(d) == "invalid", "SG6 an invalid spec: invalid")
        try:
            sign(d, "owner")
            ok(False, "SG7 an invalid spec cannot be signed")
        except ValueError as e:
            ok("not ready" in str(e), "SG7 an invalid spec cannot be signed")
    with tempfile.TemporaryDirectory() as d:
        schema.toy_spec(d)
        yaml.safe_dump(DB.example_brief(), open(os.path.join(d, DB.BRIEF), "w"))
        yaml.safe_dump({"hard_blockers": [{"id": "HB-001", "text": "t"}]}, open(os.path.join(d, DB.CRITERIA), "w"))
        rec = sign(d, "owner")
        ok(rec.get("producer") == "scripts/decision_brief.py" and rec["sha256"].get(SPEC) and status(d) == "signed"
           and DB.status(d) == "signed", f"SG8 with a brief, the brief machinery signs and freezes the spec: {rec}")
        with open(os.path.join(d, SPEC), "a") as f:
            f.write("\n# edited\n")
        ok(DB.status(d) == "changed", "SG9 a spec edit after the brief's sign-off shows as a changed brief sign-off")
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d)
        raw = yaml.safe_load(open(p))
        raw["engine"] = {"auto": True}
        yaml.safe_dump(raw, open(p, "w"))
        try:
            st0 = status(d)
            sign(d, "owner")
            ok(st0 == "awaiting" and status(d) == "signed",
               f"SG10 a spec with engine: auto can be signed before the engine is chosen ({st0})")
        except ValueError as e:
            ok(False, f"SG10 a spec with engine: auto can be signed before the engine is chosen: {e}")
    # Spec-declared engine parameter values (toolkit/spec/engine_parameters.py): the sheet shows each with its source,
    # its reason and the card value it replaces; a value outside the card's range is a HIGH note the owner signs.
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d)
        raw = yaml.safe_load(open(p))
        raw["engine"] = {"ref": "toolkit.spec.engine_parameters:ToySpecEngine"}
        raw["engine_parameters"] = {
            "base": {"value": 0.3, "outside_card_range": True, "source": "a toy sweep", "reason": "toy headline"},
            "k": {"value": 0.9, "source": "a toy survey", "reason": "toy setting"}}
        yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
        md = sheet(d)
        sec = md.split("## Engine parameter values set by the spec")[-1].split("\n## ")[0] \
            if "## Engine parameter values set by the spec" in md else ""
        ok(sec and "**HIGH**" in sec and sec.count("HIGH") == 1 and "a toy sweep" in sec and "toy headline" in sec
           and "0.4" in sec and "0.6" in sec and "a toy survey" in sec and "0.8" in sec,
           f"SG11 the sheet lists each spec value (source, reason, card value) and a HIGH note for the one outside the "
           f"card's range: {sec[:300]!r}")
        raw["engine"] = {"auto": True, "candidates": ["toolkit.spec.engine_parameters:ToySpecEngine",
                                                      "toolkit.engines.contract:ToyEngine"]}
        yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
        sec = sheet(d).split("## Engine parameter values set by the spec")[-1].split("\n## ")[0]
        ok("ToySpecEngine" in sec and "ToyEngine" in sec and sec.count("**HIGH**") == 1,
           f"SG12 with engine: auto, the card values are shown per candidate engine: {sec[:300]!r}")
        # review round 2 (R2-4): a string engine reference, and engine: auto with the candidates of the pre-sign check
        def sec_of(md):
            return md.split("## Engine parameter values set by the spec")[-1].split("\n## ")[0]
        raw["engine"] = "toolkit.spec.engine_parameters:ToySpecEngine"
        yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
        sec = sec_of(sheet(d))
        ok("ToySpecEngine" in sec and sec.count("**HIGH**") == 1 and "0.4" in sec,
           f"SG13 a string engine reference shows its card values and the HIGH note: {sec[:300]!r}")
        cands = [{"ref": "toolkit.spec.engine_parameters:ToySpecEngine", "eligible": True, "why_not": []}]
        for eng in ("auto", {"auto": True}):
            raw["engine"] = eng
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            sec = sec_of(sheet(d, candidates=cands))
            ok("ToySpecEngine" in sec and sec.count("**HIGH**") == 1,
               f"SG14 engine {eng!r}: the pre-sign check's candidates give the card values: {sec[:300]!r}")
            sec = sec_of(sheet(d))
            ok("card values are shown once" in sec and "**HIGH**" not in sec,
               f"SG15 engine {eng!r} with no candidate known: the sheet says the card values are not yet known: "
               f"{sec[:300]!r}")
    # DI6, DI7, DI10 (docs/design/v2-derived-inputs.md): the sign-off freezes the derivation record, and the sheet
    # shows the owner the accepted defaults and the columns the run chose not to use.
    try:
        with tempfile.TemporaryDirectory() as d:
            schema.derived_toy_spec(d)
            rec = sign(d, "owner")
            ok(rec["sha256"].get(DERIVATIONS_RECORD) and status(d) == "signed",
               f"DI6c the sign-off freezes the derivation record's hash next to the spec's: {sorted(rec['sha256'])}")
            out = os.path.join(d, "data", "derived", "units.csv")
            with open(out, "a") as f:
                f.write("99,r0,g0,0,1000,0,0.1,x\n")
            ok(status(d) == "changed", f"DI6d an edit of a derived file after sign-off breaks the sign-off: {status(d)}")
        with tempfile.TemporaryDirectory() as d:
            schema.derived_toy_spec(d)
            import yaml as _y
            dp = os.path.join(d, "data", "derivations.yaml")
            doc = _y.safe_load(open(dp))
            doc["derivations"][0]["columns"]["ignores"] = {"data/inputs/units_raw.csv": {"elig": "not used by the toy"}}
            doc["derivations"][0]["columns"]["uses"]["data/inputs/units_raw.csv"].remove("elig")
            _y.safe_dump(doc, open(dp, "w"))
            md = sheet(d)
            ok("group" in md and "the toy units carry no group label" in md and "no group label" in md,
               f"DI7f the sign-off sheet lists each accepted default with its card meaning and the spec's reason")
            ok("elig" in md and "not used by the toy" in md and "units_raw.csv" in md,
               "DI10c the sign-off sheet lists the ignored columns per input")
    except Exception as e:  # noqa: BLE001 -- before the record and the sheet exist (step α) this is the failure
        ok(False, f"DI6/DI7/DI10 sign-off: {type(e).__name__}: {e}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["status", "sign", "sheet"])
    ap.add_argument("run_dir")
    ap.add_argument("--by", default="owner", choices=["owner", "lead"])
    ap.add_argument("--note", default="")
    a = ap.parse_args(argv)
    if a.action == "status":
        print(status(a.run_dir))
        return 0
    if a.action == "sheet":
        print(sheet(a.run_dir))
        return 0
    print(json.dumps(sign(a.run_dir, a.by, a.note), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
