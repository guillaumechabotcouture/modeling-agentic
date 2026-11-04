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
    sign-off made before the record existed, in a run that has none, is unaffected. Everything is read through
    no-follow fds (review round 3, P1-3): there is no path check to race."""
    signed = (rec.get("sha256") or {}).get(DERIVATIONS_RECORD)
    try:
        raw = read_nofollow(run_dir, DERIVATIONS_RECORD)[0]
    except FileNotFoundError:
        raw = None
    except (LinkRefused, OSError):
        return [DERIVATIONS_RECORD]
    now = hashlib.sha256(raw).hexdigest() if raw is not None else None
    if signed is None and now is None:
        return []
    if signed != now:
        return [DERIVATIONS_RECORD]
    try:
        derivs = (json.loads(raw.decode("utf-8")) or {}).get("derivations") or {}
    except (UnicodeDecodeError, ValueError, AttributeError):
        return [DERIVATIONS_RECORD]
    return _outputs_changed(run_dir, derivs)


def _outputs_changed(run_dir: str, derivs: dict) -> list[str]:
    """The listed derivation outputs whose bytes differ from the record. Each is hashed through a no-follow fd; an
    output outside the run dir, behind a link, or unreadable is a change and is never read (the record is written by
    a derivation script an agent wrote)."""
    out = []
    for e in (derivs or {}).values():
        for o, h in ((e or {}).get("outputs") or {}).items() if isinstance(e, dict) else []:
            try:
                same = _sha_nofollow(run_dir, str(o)) == h
            except (ValueError, OSError):
                same = False
            if not same:
                out.append(o)
    return sorted(out)


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
    try:
        sheet_bytes = read_nofollow(run_dir, SHEET)[0]
    except FileNotFoundError:
        sheet_bytes = None
    except LinkRefused as e:
        raise ValueError(f"{SHEET}: {e}") from None
    sheet_sha = hashlib.sha256(sheet_bytes).hexdigest() if sheet_bytes is not None else "none"
    if expect_sheet_sha256 is not None and sheet_sha != expect_sheet_sha256:
        raise ValueError(f"{SHEET} changed since it was shown: read it again before signing")
    src = sheet_sources(sheet_bytes.decode("utf-8", "replace")) if sheet_bytes is not None else None
    if sheet_bytes is not None and src is None:
        # Codex on 4d20e23: a sheet without its sources footer cannot show which spec, record and geometry evidence
        # it was generated from; it is refused, never skipped
        raise ValueError(f"{SHEET} predates source binding (no sources line); regenerate the sheet (python3 "
                         "toolkit/spec/signoff.py sheet <run_dir> --write) and read it again")
    if sheet_bytes is not None and (
            src.get("spec_sha256") != sha or src.get("derivations_sha256") != (dsha or "none")):
        raise ValueError(f"{SHEET} was generated from another spec or derivation record (it is stale): the sheet is "
                         "regenerated before the owner signs; read the new one")
    # Codex on 08f96a9: the geometry evidence the sheet showed must still be current when the freeze approves
    geo_expect = None
    if src is not None:
        geo_expect = src.get("geometry_evidence_sha256")
        if not isinstance(geo_expect, str):
            raise ValueError(f"{SHEET} records no geometry evidence (it predates the geometry check): the sheet is "
                             "regenerated before the owner signs; read the new one")
    errs = schema.validate(schema.load(os.path.join(run_dir, SPEC), data=data), pending_ok=True)
    if errs:
        raise ValueError("spec not ready: " + "; ".join(errs[:10]))
    _refuse_held_out(data, run_dir)     # also in decision_brief.sign: every entry point, whoever signs
    if _spec_record(run_dir) is not None:
        raise ValueError("the spec is already signed; a change needs a new sign-off by the owner")
    if os.path.lexists(os.path.join(run_dir, DB.BRIEF)):
        # decision_brief.sign freezes the spec's geometry too (#131)
        return DB.sign(run_dir, by, answers=answers, note=note, accept_proposed=accept_proposed,
                       expect={SPEC: sha}, extra_sha={DERIVATIONS_RECORD: dsha}, expect_geometry=geo_expect)
    # The geometry part of the unsigned presentation file and its input hashes are frozen with the spec; an owner's
    # sign-off also approves the HIGH geometry exceptions the spec proposes (docs/design/v2-input-geometry.md §2).
    # The spec is the bytes read, validated and hashed above, never read again (#129: SG15).
    import yaml
    from toolkit.spec import geometry
    try:
        spec_doc = yaml.safe_load(data.decode("utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        raise ValueError(f"{SPEC} does not parse: {e}") from None
    geometry.freeze(run_dir, by, spec=spec_doc if isinstance(spec_doc, dict) else {}, expect_evidence=geo_expect)
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


def _refuse_held_out(spec_bytes: bytes, run_dir: str) -> None:
    """Refuse (ValueError) a spec whose engine parameter values have no basis, or were chosen with a held-out benchmark
    of an engine the spec may run (toolkit/spec/engine_parameters.py signoff_refusals). No signer, flag or note
    overrides it. `spec_bytes`: the bytes being signed."""
    import yaml
    from toolkit.spec import engine_parameters as PV
    try:
        doc = yaml.safe_load(spec_bytes.decode("utf-8")) or {}
    except (yaml.YAMLError, UnicodeDecodeError) as e:
        raise ValueError(f"{SPEC} does not parse: {e}") from None
    bad = PV.signoff_refusals(doc if isinstance(doc, dict) else {}, run_dir=run_dir)
    if bad:
        raise ValueError("spec not signable (engine_parameters): " + "; ".join(bad[:10]))


SHEET_SOURCES = "<!-- signoff-sheet-sources: "


def sheet_sources(text) -> dict | None:
    """The source hashes the sheet writer recorded in the sheet ({spec_sha256, derivations_sha256}), or None."""
    if not isinstance(text, str):
        return None
    for line in reversed(text.splitlines()):
        if line.startswith(SHEET_SOURCES) and line.endswith(" -->"):
            try:
                d = json.loads(line[len(SHEET_SOURCES):-4])
            except ValueError:
                return None
            return d if isinstance(d, dict) else None
    return None


def write_sheet(run_dir: str, candidates: list | None = None) -> str:
    """Generate the sign-off sheet and publish it (no link followed, atomic), with the hashes of the spec and the
    derivation record it was generated from recorded in its last line. The spec and the record are each read once;
    the sheet is rendered from those bytes and records their hashes (review round 4, P1-4). The console disables
    approval and the signer refuses when they no longer match."""
    try:
        spec_bytes = read_nofollow(run_dir, SPEC)[0]
    except (FileNotFoundError, LinkRefused) as e:
        raise ValueError(f"{SPEC}: {e}") from None
    try:
        rec_bytes = read_nofollow(run_dir, DERIVATIONS_RECORD)[0]
    except FileNotFoundError:
        rec_bytes = None
    except LinkRefused as e:
        raise ValueError(f"{DERIVATIONS_RECORD}: {e}") from None
    # The input geometry check, re-run on the spec bytes read above, then shown from the record it wrote; its evidence
    # hash (declaration plus boundary, crosswalk and units sha256: what an approval binds to) is a recorded source,
    # so the signer refuses when the geometry inputs changed after the owner saw the sheet (Codex on 08f96a9).
    import yaml
    from toolkit.spec import geometry
    try:
        spec_doc = yaml.safe_load(spec_bytes.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError):
        spec_doc = None
    if isinstance(spec_doc, dict):
        geometry.run(run_dir, spec_doc)
    section = geometry.sheet_section(run_dir)
    text = sheet(run_dir, candidates, spec_bytes=spec_bytes, record_bytes=rec_bytes, geometry_section=section)
    src = json.dumps({"spec_sha256": hashlib.sha256(spec_bytes).hexdigest(),
                      "derivations_sha256": hashlib.sha256(rec_bytes).hexdigest() if rec_bytes is not None else "none",
                      "geometry_evidence_sha256": section[1]},
                     sort_keys=True)
    write_nofollow(run_dir, SHEET, (text.rstrip("\n") + f"\n\n{SHEET_SOURCES}{src} -->\n").encode(), atomic=True)
    return os.path.join(run_dir, SHEET)


def sheet(run_dir: str, candidates: list | None = None, spec_bytes: bytes | None = None,
          record_bytes: bytes | None = None, geometry_section: tuple | None = None) -> str:
    """The sign-off sheet (markdown): what signing accepts beyond the spec's text. Each engine default the spec
    accepts by name, with the card's meaning and the spec's reason; the staged columns and tables the derivations
    do not use, with their reasons; the derivations; each engine parameter value the spec sets, with the card value
    it replaces and a HIGH note for one outside the card's range; and, when given, the candidate engines and why any
    is ineligible ({ref, eligible, why_not} rows from the pre-sign check)."""
    import yaml
    path = os.path.join(run_dir, SPEC)
    try:
        if spec_bytes is None:
            with open(path, "rb") as f:
                spec_bytes = f.read()
            record_bytes = record_bytes if record_bytes is not None else (
                open(os.path.join(run_dir, DERIVATIONS_RECORD), "rb").read()
                if os.path.exists(os.path.join(run_dir, DERIVATIONS_RECORD)) else None)
        raw = yaml.safe_load(spec_bytes.decode("utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        return f"# Decision spec sign-off sheet\n\n{SPEC} unreadable: {e}\n"
    rsha = hashlib.sha256(record_bytes).hexdigest() if record_bytes is not None else "none"
    out = ["# Decision spec sign-off sheet", "",
           f"Signing accepts `{SPEC}` (sha256 `{hashlib.sha256(spec_bytes).hexdigest()[:16]}`) and the derived data "
           f"recorded in `{DERIVATIONS_RECORD}` (sha256 `{rsha[:16]}`). "
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
    if geometry_section is None:
        from toolkit.spec import geometry
        geometry_section = geometry.sheet_section(run_dir)
    out += geometry_section[0]                 # the input geometry check and the findings accepted by name
    from toolkit.spec import engine_parameters as PV
    out += PV.sheet_lines(raw, candidates, run_dir)     # each spec value, the card value it replaces, HIGH when outside
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
        write_sheet(d)                 # with a spec, a brief sign-off binds the sheet's geometry evidence (#131)
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
        write_sheet(d)        # round 3: the sheet records the sources it was made from
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
    # SG19-SG22 (run console review round 3)
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d)
        try:
            write_sheet(d)
            src = sheet_sources(open(os.path.join(d, SHEET)).read())
            ok(src and src.get("spec_sha256") == DB._sha(p) and src.get("derivations_sha256") == "none",
               f"SG19 the sheet writer records the source hashes it was generated from ({src})")
            with open(p, "a") as f:
                f.write("\n# a newer spec\n")
            try:
                sign(d, "owner", note="n", expect_sha256=DB._sha(p),
                     expect_sheet_sha256=DB._sha(os.path.join(d, SHEET)), expect_derivations_sha256="none")
                ok(False, "SG20 a sheet generated from an older spec cannot authorise the newer one")
            except ValueError as e:
                ok("sheet" in str(e) and status(d) == "awaiting",
                   f"SG20 a sheet generated from an older spec cannot authorise the newer one ({e})")
        except NameError as e:
            ok(False, f"SG19-SG20 sheet sources: {e}")
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        schema.toy_spec(d)
        victim = os.path.join(out, "victim")
        open(victim, "w").write("untouched")
        os.symlink(victim, os.path.join(d, SHEET))
        try:
            write_sheet(d)
        except (ValueError, OSError, NameError):
            pass
        ok(open(victim).read() == "untouched", "SG21 the sheet is not written through a link")
    with tempfile.TemporaryDirectory() as d:
        schema.toy_spec(d)
        yaml.safe_dump(DB.example_brief(), open(os.path.join(d, DB.BRIEF), "w"))
        yaml.safe_dump({"hard_blockers": [{"id": "HB-001", "text": "t"}]}, open(os.path.join(d, DB.CRITERIA), "w"))
        opens, real_open, real_osopen = [], builtins.open, os.open
        builtins.open = lambda f, *a, **k: (opens.append(f) if str(f).endswith(DB.CRITERIA) else None) or \
            real_open(f, *a, **k)
        os.open = lambda f, *a, **k: (opens.append(f) if str(f).endswith(DB.CRITERIA) else None) or \
            real_osopen(f, *a, **k)
        try:
            DB.sign(d, "owner", note="n")
        except (ValueError, OSError):
            pass
        finally:
            builtins.open, os.open = real_open, real_osopen
        ok(len(opens) == 1, f"SG22 the brief sign-off reads success_criteria.yaml once ({len(opens)} opens)")
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        schema.toy_spec(d)
        open(os.path.join(out, "x.csv"), "w").write("secret\n")
        os.symlink(out, os.path.join(d, "data"))
        hashed, real_sha, real_rp = [], DB._sha, os.path.realpath
        DB._sha = lambda p: hashed.append(p) or real_sha(p)
        os.path.realpath = lambda p, *a, **k: os.path.abspath(p)       # the check sees no link (a race)
        try:
            bad = _outputs_changed(d, {"u": {"outputs": {"data/x.csv": "ab"}}})
        except NameError as e:
            bad = e
        finally:
            DB._sha, os.path.realpath = real_sha, real_rp
        ok(not isinstance(bad, NameError) and "data/x.csv" in bad and not any("x.csv" in str(h) for h in hashed),
           f"SG23 derivation outputs are hashed through no-follow fds: a linked data/ is a change, never read ({bad})")
    # SG24 (round 4, P1-4): the sheet is rendered from the same captured spec bytes it records the hash of
    with tempfile.TemporaryDirectory() as d:
        schema.toy_spec(d)
        opens, real_open, real_osopen = [], builtins.open, os.open
        builtins.open = lambda f, *a, **k: (opens.append(f) if str(f).endswith(SPEC) else None) or real_open(f, *a, **k)
        os.open = lambda f, *a, **k: (opens.append(f) if str(f).endswith(SPEC) else None) or real_osopen(f, *a, **k)
        try:
            write_sheet(d)
        finally:
            builtins.open, os.open = real_open, real_osopen
        ok(len(opens) == 1, f"SG24 write_sheet reads the spec once and renders and hashes those bytes ({len(opens)})")
    from toolkit import import_guard as _G
    from toolkit.spec import toy_engines as _TOYS
    _G.allow_test_refs("engine", _TOYS.REFS)       # the self-test toy engines (refused in production)
    # Spec-declared engine parameter values (toolkit/spec/engine_parameters.py): the sheet shows each with its source,
    # its reason and the card value it replaces; a value outside the card's range is a HIGH note the owner signs.
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d)
        raw = yaml.safe_load(open(p))
        raw["engine"] = {"ref": "toolkit.spec.toy_engines:ToySpecEngine"}
        raw["engine_parameters"] = {
            "base": {"value": 0.3, "outside_card_range": True, "source": "a toy sweep", "reason": "toy headline",
                     "basis": "owner_judgement"},
            "k": {"value": 0.9, "source": "a toy survey", "reason": "toy setting", "basis": "setting_data"}}
        yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
        md = sheet(d)
        sec = md.split("## Engine parameter values set by the spec")[-1].split("\n## ")[0] \
            if "## Engine parameter values set by the spec" in md else ""
        ok(sec and "**HIGH**" in sec and sec.count("HIGH") == 1 and "a toy sweep" in sec and "toy headline" in sec
           and "0.4" in sec and "0.6" in sec and "a toy survey" in sec and "0.8" in sec,
           f"SV1 the sheet lists each spec value (source, reason, card value) and a HIGH note for the one outside the "
           f"card's range: {sec[:300]!r}")
        raw["engine"] = {"auto": True, "candidates": ["toolkit.spec.toy_engines:ToySpecEngine",
                                                      "toolkit.engines.contract:ToyEngine"]}
        yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
        sec = sheet(d).split("## Engine parameter values set by the spec")[-1].split("\n## ")[0]
        ok("ToySpecEngine" in sec and "ToyEngine" in sec and sec.count("**HIGH**") == 1,
           f"SV2 with engine: auto, the card values are shown per candidate engine: {sec[:300]!r}")
        # review round 2 (R2-4): a string engine reference, and engine: auto with the candidates of the pre-sign check
        def sec_of(md):
            return md.split("## Engine parameter values set by the spec")[-1].split("\n## ")[0]
        raw["engine"] = "toolkit.spec.toy_engines:ToySpecEngine"
        yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
        sec = sec_of(sheet(d))
        ok("ToySpecEngine" in sec and sec.count("**HIGH**") == 1 and "0.4" in sec,
           f"SV3 a string engine reference shows its card values and the HIGH note: {sec[:300]!r}")
        cands = [{"ref": "toolkit.spec.toy_engines:ToySpecEngine", "eligible": True, "why_not": []}]
        for eng in ("auto", {"auto": True}):
            raw["engine"] = eng
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            sec = sec_of(sheet(d, candidates=cands))
            ok("ToySpecEngine" in sec and sec.count("**HIGH**") == 1,
               f"SV4 engine {eng!r}: the pre-sign check's candidates give the card values: {sec[:300]!r}")
            sec = sec_of(sheet(d))
            ok("card values are shown once" in sec and "**HIGH**" not in sec,
               f"SV5 engine {eng!r} with no candidate known: the sheet says the card values are not yet known: "
               f"{sec[:300]!r}")
        # review round 3 (R3-4): a pre-sign candidate's configuration is kept, so the right card is loaded
        raw["engine"] = {"auto": True}
        yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
        sec = sec_of(sheet(d, candidates=[{"ref": "toolkit.engines.empirical.engine:PackageRateEngine",
                                           "config": {"components": ["a", "b"]}, "eligible": False, "why_not": []}]))
        ok("PackageRateEngine`: not a parameter of this engine" in sec and "card not loaded" not in sec,
           f"SV6 a candidate's configuration is kept when its card is loaded: {sec[:400]!r}")
        # review round 4 (R4-2): the spec lists the candidates and the pre-sign check supplies their configurations
        raw["engine"] = {"auto": True, "candidates": ["toolkit.spec.toy_engines:ToySpecEngine",
                                                      "toolkit.engines.empirical.engine:PackageRateEngine"]}
        yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
        sec = sec_of(sheet(d, candidates=[{"ref": "toolkit.engines.empirical.engine:PackageRateEngine",
                                           "config": {"components": ["a", "b"]}, "eligible": False, "why_not": []}]))
        ok("PackageRateEngine`: not a parameter of this engine" in sec and "card not loaded" not in sec
           and "ToySpecEngine`: card value" in sec,
           f"SV7 the spec's candidate refs are merged with the supplied candidate configurations: {sec[:500]!r}")
    # SV8-SV10: a spec value chosen with a held-out benchmark (toolkit/spec/engine_parameters.py signoff_refusals) is
    # refused by every entry point, whoever signs. ToySpecEngine's pack: fit targets TF1, TF2; held out TH1, TH2.
    import subprocess
    toy = "toolkit.spec.toy_engines:ToySpecEngine"
    for by in ("owner", "lead"):
        with tempfile.TemporaryDirectory() as d:
            p = schema.toy_spec(d)
            raw = yaml.safe_load(open(p))
            raw["engine"] = {"auto": True, "candidates": [toy]}        # validate(pending_ok) checks the shape only
            raw["engine_parameters"] = {"k": {"value": 0.9, "source": "a toy sweep", "reason": "toy headline",
                                              "basis": "fitted", "fitted_to": ["TH1"]}}
            yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
            pend = schema.validate(schema.load(p), pending_ok=True)
            try:
                sign(d, by)
                why = None
            except ValueError as e:
                why = str(e)
            ok(not pend and why and "held-out benchmark" in why and status(d) != "signed"
               and not os.path.exists(os.path.join(d, SPEC_SIGNOFF)),
               f"SV8 ({by}) engine: auto: a value fitted to a candidate's held-out benchmark is refused at sign-off even "
               f"though the pending checks pass ({pend[:2]}): {why}")
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d)
        raw = yaml.safe_load(open(p))
        raw["engine"] = {"ref": toy}
        raw["engine_parameters"] = {"k": {"value": 0.9, "source": "a toy sweep", "basis": "owner_judgement",
                                          "reason": "the value at which TH2 is in band"}}
        yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
        sec = sheet(d).split("## Engine parameter values set by the spec")[-1].split("\n## ")[0]
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from toolkit.spec import toy_engines as T; "
                "T.enable_for_test(); from toolkit.spec import signoff as S; sys.exit(S.main(sys.argv[2:]))")
        r = subprocess.run([sys.executable, "-c", code, ROOT, "sign", d, "--by", "owner", "--note", "accept"],
                           capture_output=True, text=True, timeout=300)
        ok("**REFUSED**" in sec and "TH2" in sec and "no sign-off, the owner's included" in sec
           and r.returncode != 0 and "held-out benchmark" in r.stderr and status(d) != "signed",
           f"SV9 the sheet shows the refusal and the signoff CLI refuses it with --by owner (exit {r.returncode}): "
           f"{r.stderr[-300:]!r}; {sec[-300:]!r}")
        raw["engine_parameters"]["k"]["reason"] = "the owner's planning value for this setting"
        yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
        sec = sheet(d).split("## Engine parameter values set by the spec")[-1].split("\n## ")[0]
        sign(d, "owner")
        ok("REFUSED" not in sec and "Basis: owner_judgement" in sec and status(d) == "signed",
           f"SV10 without the held-out benchmark the value signs and the sheet shows its basis: {sec[:300]!r}")
    # DI6, DI7, DI10 (docs/design/v2-derived-inputs.md): the sign-off freezes the derivation record, and the sheet
    # shows the owner the accepted defaults and the columns the run chose not to use.
    try:
        with tempfile.TemporaryDirectory() as d:
            schema.derived_toy_spec(d)
            rec = sign(d, "owner")
            ok(rec["sha256"].get(DERIVATIONS_RECORD) and status(d) == "signed",
               f"DI6c the sign-off freezes the derivation record's hash next to the spec's: {sorted(rec['sha256'])}")
            try:
                g = json.load(open(os.path.join(d, "data", "geometry_signoff.json")))
            except (OSError, ValueError):
                g = {}
            ok(g.get("signed_by") == "owner" and (g.get("declaration") or {}).get("enabled") is False,
               f"IG-S2 the sign-off also freezes the geometry declaration (here: no boundaries, check not enabled) in "
               f"the harness's geometry record ({g.get('declaration')})")
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
            ok("## Input geometry check" in md and "has not run" in md,
               "IG-S1a with no geometry record the sheet says the check has not run (never a silent pass)")
            from toolkit.spec import geometry
            geometry.toy_inputs(d)                   # its own spec and units in the same dir: the sheet reads the record
            sp = _y.safe_load(open(os.path.join(d, SPEC)))
            sp["decision_units"][geometry.ACCEPT_KEY] = {"reciprocal_swap:swap_a+swap_b": "rows checked by the owner"}
            geometry.write(d, geometry.check(d, sp, {"edge_tolerance_km": 5.0, "area_ratio_max": 1.5}))
            md = sheet(d)
            ok("reciprocal_swap:swap_a+swap_b" in md and "rows checked by the owner" in md
               and "1 reciprocal swap(s)" in md and "point_outside_own_polygon:stray" in md,
               "IG-S1b the sheet lists the counts, each geometry finding accepted by name with its reason, and the "
               "open ones")
    except Exception as e:  # noqa: BLE001 -- before the record and the sheet exist (step α) this is the failure
        ok(False, f"DI6/DI7/DI10 sign-off: {type(e).__name__}: {e}")
    # IG-S3 (Codex on 08f96a9, P1): the sheet records the geometry evidence it shows; a boundary change after the
    # sheet was generated refuses the sign-off, so no HIGH exception is approved against evidence nobody saw
    try:
        from toolkit.spec import geometry
        with tempfile.TemporaryDirectory() as d:
            geometry.toy_spec_with_boundaries(d, accept={"reciprocal_swap:0+1": "rows checked by the owner"})
            write_sheet(d)
            src = sheet_sources(open(os.path.join(d, SHEET)).read()) or {}
            has = src.get("geometry_evidence_sha256") not in (None, "none")
            bpath = os.path.join(d, "b.geojson")
            orig = open(bpath).read()
            g = json.loads(orig)
            g["features"][5]["properties"]["note"] = "changed after the sheet"
            open(bpath, "w").write(json.dumps(g))
            try:
                sign(d, "owner", note="n", expect_sheet_sha256=DB._sha(os.path.join(d, SHEET)))
                refused = ""
            except ValueError as e:
                refused = str(e)
            open(bpath, "w").write(orig)
            rec = sign(d, "owner", note="n", expect_sheet_sha256=DB._sha(os.path.join(d, SHEET)))
            appr = json.load(open(os.path.join(d, geometry.SIGNOFF_FILE))).get("approvals") or {}
            ok(has and "geometry" in refused and rec and sorted(appr) == ["reciprocal_swap:0+1"],
               f"IG-S3 the sheet records the geometry evidence; a boundary change after it refuses the sign-off, and the "
               f"unchanged evidence signs with the approval ({has}; {refused[:90]}; {sorted(appr)})")
    except Exception as e:  # noqa: BLE001 -- before the evidence is recorded (step α)
        ok(False, f"IG-S3 the sheet binds the geometry evidence: {type(e).__name__}: {e}")
    # IG-S4 (Codex on 4d20e23, P2): a sheet with no sources footer is refused, not skipped
    try:
        from toolkit.spec import geometry
        with tempfile.TemporaryDirectory() as d:
            geometry.toy_spec_with_boundaries(d, accept={"reciprocal_swap:0+1": "rows checked by the owner"})
            geometry.toy_write_sheet(d, footer=False)
            try:
                sign(d, "owner", note="n")
                why = ""
            except ValueError as e:
                why = str(e)
            ok("predates source binding" in why and status(d) == "awaiting",
               f"IG-S4 a sheet without its sources footer is refused: it predates source binding ({why[:90]})")
    except Exception as e:  # noqa: BLE001
        ok(False, f"IG-S4 a sheet without its sources footer is refused: {type(e).__name__}: {e}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("action", choices=["status", "sign", "sheet"])
    ap.add_argument("run_dir")
    ap.add_argument("--by", default=None, choices=["owner", "lead"])
    ap.add_argument("--note", default="")
    ap.add_argument("--write", action="store_true", help="sheet: regenerate and publish the sheet (with its sources "
                                                         "line) instead of printing it")
    a = ap.parse_args(argv)
    if a.action == "sign" and a.by is None:
        ap.error("sign needs --by owner or --by lead")
    if a.action == "status":
        print(status(a.run_dir))
        return 0
    if a.action == "sheet":
        print(write_sheet(a.run_dir) if a.write else sheet(a.run_dir))
        return 0
    print(json.dumps(sign(a.run_dir, a.by, a.note), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
