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
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (ROOT, os.path.join(ROOT, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import decision_brief as DB  # noqa: E402
from lib.safe_io import LinkRefused, read_nofollow, write_nofollow  # noqa: E402
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
    out = []
    for e in derivs.values():
        for o, h in ((e or {}).get("outputs") or {}).items():
            # The record is written by a derivation script an agent wrote: an output outside the run dir is a change
            # (it cannot have been derived here) and is never hashed (review round 2, P1-4).
            if not _inside(run_dir, str(o)) or DB._sha(os.path.join(run_dir, o)) != h:
                out.append(o)
    return sorted(out)


def _inside(run_dir: str, rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if not rel or os.path.isabs(rel) or ".." in parts:
        return False
    root = os.path.realpath(run_dir)
    return os.path.realpath(os.path.join(root, rel)).startswith(root + os.sep)


def _sha_nofollow(run_dir: str, rel: str) -> str | None:
    """sha256 of run_dir/rel read through a no-follow fd; None when absent; ValueError for a link."""
    try:
        return hashlib.sha256(read_nofollow(run_dir, rel)[0]).hexdigest()
    except FileNotFoundError:
        return None
    except LinkRefused as e:
        raise ValueError(f"{rel}: {e}") from None


def signed_sha(run_dir: str) -> str | None:
    rec = _spec_record(run_dir)
    return (rec.get("sha256") or {}).get(SPEC) if rec else None


def sign(run_dir: str, by: str, note: str = "", answers: dict | None = None, accept_proposed: bool = False,
         expect_sha256: str | None = None, expect_derivations_sha256: str | None = None,
         expect_sheet_sha256: str | None = None) -> dict:
    """Sign the spec. The expect_* hashes are what the owner was shown (the run console's approval snapshot): the
    spec, the derivation record ("none": there was none) and the sign-off sheet; a mismatch is refused. The spec is
    read once: validated, hashed and copied from those bytes; nothing is read or written through a link; the record
    is published atomically."""
    try:
        data = read_nofollow(run_dir, SPEC)[0]
    except FileNotFoundError:
        raise ValueError(f"{SPEC} does not exist") from None
    except LinkRefused as e:
        raise ValueError(f"{SPEC}: {e}") from None
    sha = hashlib.sha256(data).hexdigest()
    if expect_sha256 is not None and sha != expect_sha256:
        raise ValueError(f"{SPEC} changed since it was shown (sha256 {sha[:12]}, shown {str(expect_sha256)[:12]}): "
                         "read it again before signing")
    dsha = _sha_nofollow(run_dir, DERIVATIONS_RECORD)
    if expect_derivations_sha256 is not None and (dsha or "none") != expect_derivations_sha256:
        raise ValueError(f"{DERIVATIONS_RECORD} changed since the sheet was shown: read it again before signing")
    if expect_sheet_sha256 is not None and (_sha_nofollow(run_dir, SHEET) or "none") != expect_sheet_sha256:
        raise ValueError(f"{SHEET} changed since it was shown: read it again before signing")
    errs = schema.validate(schema.load(os.path.join(run_dir, SPEC), data=data), pending_ok=True)
    if errs:
        raise ValueError("spec not ready: " + "; ".join(errs[:10]))
    if _spec_record(run_dir) is not None:
        raise ValueError("the spec is already signed; a change needs a new sign-off by the owner")
    if os.path.lexists(os.path.join(run_dir, DB.BRIEF)):
        return DB.sign(run_dir, by, answers=answers, note=note, accept_proposed=accept_proposed,
                       expect={SPEC: sha}, extra_sha={DERIVATIONS_RECORD: dsha})
    record = {"signed_by": by, "signed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(), "note": note,
              "sha256": {SPEC: sha, DERIVATIONS_RECORD: dsha},
              "producer": "toolkit/spec/signoff.py",
              "schema_version": schema.SCHEMA_VERSION}
    try:
        write_nofollow(run_dir, f"{DB.SIGNED_DIR}/{SPEC}", data, atomic=True)   # the bytes that were hashed
        write_nofollow(run_dir, SPEC_SIGNOFF, json.dumps(record, indent=1).encode(), atomic=True)
    except (LinkRefused, OSError) as e:
        raise ValueError(f"sign-off not written: {e}") from None
    return record


def sheet(run_dir: str, candidates: list | None = None) -> str:
    """The sign-off sheet (markdown): what signing accepts beyond the spec's text. Each engine default the spec
    accepts by name, with the card's meaning and the spec's reason; the staged columns and tables the derivations
    do not use, with their reasons; the derivations; and, when given, the candidate engines and why any is
    ineligible ({ref, eligible, why_not} rows from the pre-sign check)."""
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
    # SG11-SG12 (run console review, finding 6): a sign-off names the spec it was shown; another spec is refused.
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d)
        shown = DB._sha(p)
        with open(p, "a") as f:
            f.write("\n# edited after the owner read it\n")
        try:
            sign(d, "owner", note="n", expect_sha256=shown)
            ok(False, "SG11 a sign-off for a spec other than the one shown is refused")
        except (ValueError, TypeError) as e:
            ok(isinstance(e, ValueError) and "changed" in str(e) and status(d) == "awaiting",
               f"SG11 a sign-off for a spec other than the one shown is refused ({e})")
        try:
            rec = sign(d, "owner", note="n", expect_sha256=DB._sha(p))
            ok(rec["sha256"][SPEC] == DB._sha(p) and status(d) == "signed",
               "SG12 the sign-off records the hash of the bytes it checked")
        except (ValueError, TypeError) as e:
            ok(False, f"SG12 the sign-off records the hash of the bytes it checked ({e})")
    # SG13-SG17 (run console review round 2)
    import builtins
    for target in ("signed_dir", "record"):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
            schema.toy_spec(d)
            victim = os.path.join(out, "victim")
            open(victim, "w").write("untouched")
            if target == "signed_dir":
                os.symlink(out, os.path.join(d, DB.SIGNED_DIR))
            else:
                os.symlink(victim, os.path.join(d, SPEC_SIGNOFF))
            try:
                sign(d, "owner", note="n")
            except (ValueError, OSError):
                pass
            ok(open(victim).read() == "untouched" and sorted(os.listdir(out)) == ["victim"],
               f"SG13 the sign-off is not written through a linked {target} ({sorted(os.listdir(out))})")
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        schema.toy_spec(d)
        os.makedirs(os.path.join(d, "data"))
        private = os.path.join(out, "private.csv")
        open(private, "w").write("secret\n")
        rel = os.path.relpath(private, d)
        json.dump({"derivations": {"u": {"outputs": {rel: "ab"}}}}, open(os.path.join(d, DERIVATIONS_RECORD), "w"))
        hashed, real_sha = [], DB._sha
        DB._sha = lambda p: hashed.append(os.path.realpath(p)) or real_sha(p)
        try:
            bad = derived_changed(d, {"sha256": {DERIVATIONS_RECORD: real_sha(os.path.join(d, DERIVATIONS_RECORD))}})
        finally:
            DB._sha = real_sha
        ok(rel in bad and os.path.realpath(private) not in hashed,
           f"SG14 a derivation output outside the run dir is reported changed and never hashed ({bad})")
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d)
        shown = DB._sha(p)
        opens, real_open, real_osopen = [], builtins.open, os.open

        def bopen(f, *a, **k):
            if str(f).endswith(SPEC):
                opens.append(str(f))
            return real_open(f, *a, **k)

        def oopen(f, *a, **k):
            if str(f).endswith(SPEC):
                opens.append(str(f))
            return real_osopen(f, *a, **k)
        builtins.open, os.open = bopen, oopen
        try:
            sign(d, "owner", note="n", expect_sha256=shown)
        except Exception as e:  # noqa: BLE001
            opens.append(f"error {e}")
        finally:
            builtins.open, os.open = real_open, real_osopen
        ok(len(opens) == 1, f"SG15 the spec is read once: validated, hashed and copied from the same bytes ({opens})")
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d)
        open(os.path.join(d, SHEET), "w").write("sheet\n")
        sh = DB._sha(p)
        res = []
        for kw in ({"expect_derivations_sha256": "0" * 64}, {"expect_sheet_sha256": "0" * 64}):
            try:
                sign(d, "owner", note="n", expect_sha256=sh, **kw)
                res.append(False)
            except (ValueError, TypeError) as e:
                res.append(isinstance(e, ValueError))
        ok(all(res) and status(d) == "awaiting",
           f"SG16 the sign-off checks the derivation record's and the sheet's sha256 too ({res})")
        try:
            sign(d, "owner", note="n", expect_sha256=sh, expect_derivations_sha256="none",
                 expect_sheet_sha256=DB._sha(os.path.join(d, SHEET)))
            ok(status(d) == "signed", "SG17 with every sha matching the spec is signed")
        except (ValueError, TypeError) as e:
            ok(False, f"SG17 with every sha matching the spec is signed ({e})")
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        schema.toy_spec(d)
        yaml.safe_dump(DB.example_brief(), open(os.path.join(d, DB.BRIEF), "w"))
        yaml.safe_dump({"hard_blockers": [{"id": "HB-001", "text": "t"}]}, open(os.path.join(d, DB.CRITERIA), "w"))
        os.symlink(out, os.path.join(d, DB.SIGNED_DIR))
        opens, real_open = [], builtins.open
        builtins.open = lambda f, *a, **k: (opens.append(f) if str(f).endswith(SPEC) else None) or real_open(f, *a, **k)
        try:
            DB.sign(d, "owner", note="n")
        except (ValueError, OSError):
            pass
        finally:
            builtins.open = real_open
        ok(os.listdir(out) == [] and len(opens) <= 1,
           f"SG18 decision_brief.sign hashes and copies the same bytes, never through a linked signed dir "
           f"({os.listdir(out)}, {len(opens)} opens)")
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
