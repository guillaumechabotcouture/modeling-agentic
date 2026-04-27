#!/usr/bin/env python3
"""
Phase 9 Commit ρ — write-time figure validator.

The 1935 (fig07 +105% vs body 2%), 1721 (stale labels), and 0013
(D-022 stale AIC, D-023 stale calibration scatter) malaria runs all
shipped a figure whose visual content was inconsistent with its
caption or its source data. Phase 8 Commit ξ told downstream agents
to view PNGs via the Read tool's multimodal support, but that change
is prompt-only — agents didn't reliably do so, and the 0013 run
caught D-022 / D-023 only in round 8 with no rounds left to
regenerate.

This script makes figure freshness a write-time invariant the
modeler's own code enforces. After every plt.savefig(), the modeler
calls:

    validate_figure(
        png_path,
        source_data_paths=["data/lga_pfpr.csv", ...],
        expected_annotations=["RMSE = 0.66pp", "n=53 LGAs", ...],
    )

This writes a sidecar `<png_path>.provenance.json` with:
- content hashes of each source CSV at the time the PNG was written,
- the annotation strings the modeler asserts appear in the figure,
- the absolute path of the script that produced the PNG,
- the wall-clock time the figure was written.

Two checks are enforced:

1. Annotation presence — each expected_annotation must appear as a
   substring in the calling script's source text. The modeler
   asserts "this figure currently says X"; the script's source must
   support that. If a later regeneration silently flips a title from
   "H1 SUPPORTED" to "H1 NULL", the assertion fails because the new
   script no longer contains "H1 SUPPORTED".

2. Source data freshness — the rigor gate
   (`scripts/validate_critique_yaml.py::_check_figure_validator`)
   recomputes hashes of each source CSV at gate time. If any hash
   differs from the stored sidecar hash, the figure is stale (the
   underlying data changed after the PNG was written) and HIGH
   `figure_staleness_detected` fires before STAGE 7 — early enough
   to regenerate.

Usage:
    python3 scripts/figure_validator.py --self-test
    # In modeler scripts:
    from figure_validator import validate_figure
    plt.savefig(path)
    validate_figure(path, source_data_paths=[...], expected_annotations=[...])
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import inspect
import json
import os
import re
import sys


class FigureStalenessError(Exception):
    """Raised when a figure's recorded source-data hash no longer matches
    the current content of the source CSV. Indicates the data was
    edited after the figure was generated, but the figure was not
    regenerated. The 0013 D-022 / D-023 failure mode."""


class FigureAnnotationMismatchError(Exception):
    """Raised when an expected_annotation declared in validate_figure()
    does not appear as a substring of the calling script's source text.
    Catches the modeler asserting one thing about the figure (an
    annotation, title, or label) when the script that drew it actually
    says something different."""


def _hash_file(path: str) -> str:
    """SHA-256 content hash of a file. Returns an empty string when the
    file is missing — the gate treats missing source files as a
    distinct kind of staleness."""
    if not os.path.exists(path):
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _provenance_path(png_path: str) -> str:
    return png_path + ".provenance.json"


def _strip_validate_figure_calls(src: str) -> str:
    """Remove every `validate_figure(...)` call's character span from
    `src`, preserving the rest of the file. Necessary because the
    annotation literal we're testing is itself an argument to
    validate_figure — searching the raw source would always find it,
    defeating the check. Uses balanced-paren scanning that respects
    string literals so a `)` inside a quoted annotation doesn't
    prematurely end the call."""
    out: list[str] = []
    i = 0
    n = len(src)
    while i < n:
        m = re.match(r"validate_figure\s*\(", src[i:])
        if m:
            j = i + m.end()
            depth = 1
            while j < n and depth > 0:
                ch = src[j]
                if ch in ('"', "'"):
                    quote = ch
                    triple = src[j:j+3] == quote * 3
                    if triple:
                        j += 3
                        end = src.find(quote * 3, j)
                        j = end + 3 if end != -1 else n
                    else:
                        j += 1
                        while j < n and src[j] != quote:
                            if src[j] == "\\" and j + 1 < n:
                                j += 2
                                continue
                            j += 1
                        j += 1
                elif ch == "(":
                    depth += 1
                    j += 1
                elif ch == ")":
                    depth -= 1
                    j += 1
                else:
                    j += 1
            i = j
            continue
        out.append(src[i])
        i += 1
    return "".join(out)


def _caller_script_path() -> str | None:
    """Walk up the call stack to find the .py file that called
    validate_figure(). Skip our own frame and any internal frames so
    that the caller is the actual figure-generation script
    (e.g., model_figures.py), not validate_figure itself."""
    for frame in inspect.stack()[1:]:
        path = frame.filename
        if path and path.endswith(".py") and os.path.basename(path) != os.path.basename(__file__):
            return path
    return None


def validate_figure(png_path: str,
                    source_data_paths: list[str] | None = None,
                    expected_annotations: list[str] | None = None,
                    expected_n_data_points: int | None = None) -> None:
    """Record figure provenance and assert annotation correctness.

    Writes (or overwrites) `<png_path>.provenance.json` with the
    current content hash of each source CSV, the declared
    annotations, the caller script's absolute path, and a UTC
    timestamp. Asserts each `expected_annotations` string appears as
    a substring of the caller script's source text — raises
    FigureAnnotationMismatchError on mismatch.

    The source-data staleness check is intentionally NOT performed
    here (the typical caller IS the figure's first writer, so its
    hash is already current). Staleness is detected at gate time by
    `validate_critique_yaml._check_figure_validator`, which re-hashes
    each source CSV and compares against the recorded sidecar.

    Parameters
    ----------
    png_path
        Absolute path to the PNG that was just written.
    source_data_paths
        List of CSV / parquet / yaml files this figure was rendered
        from. The validator hashes each. Empty list is allowed but
        means staleness cannot be detected.
    expected_annotations
        List of strings the modeler asserts appear inside the
        figure (titles, legends, overlay text, axis labels). Each
        must appear as a substring of the calling script's source.
    expected_n_data_points
        Optional sanity check, recorded in the sidecar for
        downstream consumers. Not asserted here.
    """
    if not os.path.exists(png_path):
        raise FileNotFoundError(
            f"validate_figure called for {png_path}, which does not "
            "exist. Call validate_figure AFTER plt.savefig completes."
        )

    source_data_paths = source_data_paths or []
    expected_annotations = expected_annotations or []

    caller_script = _caller_script_path()
    if expected_annotations:
        if caller_script is None or not os.path.exists(caller_script):
            raise FigureAnnotationMismatchError(
                f"Cannot resolve caller script for {png_path}; "
                "annotation assertions require validate_figure to be "
                "called from a .py script."
            )
        with open(caller_script) as f:
            script_src = f.read()
        # Strip every validate_figure(...) call from the source before
        # searching — otherwise the annotation literal we passed in as
        # an argument would always satisfy itself.
        searchable = _strip_validate_figure_calls(script_src)
        missing = [a for a in expected_annotations if a not in searchable]
        if missing:
            raise FigureAnnotationMismatchError(
                f"Figure {os.path.basename(png_path)}: "
                f"declared annotation(s) {missing!r} not found in "
                f"calling script {caller_script}. Either the figure "
                f"text changed (regenerate the PNG and update the "
                f"validate_figure call) or the assertion is wrong "
                f"(remove it). Annotation strings must appear as "
                f"literal substrings in the script that drew the figure."
            )

    source_hashes = {p: _hash_file(p) for p in source_data_paths}

    sidecar = {
        "png_path": os.path.abspath(png_path),
        "generator_script": (os.path.abspath(caller_script)
                             if caller_script else None),
        "source_hashes": source_hashes,
        "expected_annotations": list(expected_annotations),
        "expected_n_data_points": expected_n_data_points,
        "written_at_utc": datetime.datetime.utcnow().isoformat() + "Z",
    }
    with open(_provenance_path(png_path), "w") as f:
        json.dump(sidecar, f, indent=2, sort_keys=True)


def check_staleness(png_path: str) -> dict:
    """Re-hash each source recorded in `<png_path>.provenance.json`
    and compare against the stored hash. Returns a dict:

        {
          "status": "fresh" | "stale" | "missing_provenance",
          "stale_sources": [<paths whose hash changed>],
          "missing_sources": [<paths that no longer exist>],
        }

    `stale` means at least one source's content hash now differs from
    what was recorded when the PNG was written — i.e., the figure
    needs regenerating. Used by the rigor-gate _check_figure_validator
    to surface staleness before STAGE 7.
    """
    sp = _provenance_path(png_path)
    if not os.path.exists(sp):
        return {"status": "missing_provenance",
                "stale_sources": [], "missing_sources": []}
    with open(sp) as f:
        sidecar = json.load(f)
    stale = []
    missing = []
    for path, recorded_hash in (sidecar.get("source_hashes") or {}).items():
        if not os.path.exists(path):
            missing.append(path)
            continue
        current = _hash_file(path)
        if current != recorded_hash:
            stale.append(path)
    if stale or missing:
        return {"status": "stale",
                "stale_sources": stale, "missing_sources": missing}
    return {"status": "fresh",
            "stale_sources": [], "missing_sources": []}


def _run_self_test() -> int:
    import tempfile

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    with tempfile.TemporaryDirectory() as d:
        # Write a fake source CSV and a fake PNG.
        src = os.path.join(d, "src.csv")
        with open(src, "w") as f:
            f.write("zone,pfpr\nNW,0.309\nNE,0.196\n")
        png = os.path.join(d, "fig.png")
        with open(png, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        # Write a synthetic caller script with the annotation literal
        # baked into a plt.title call. We invoke validate_figure FROM
        # that script so inspect.stack() picks it up as the caller.
        caller = os.path.join(d, "make_figure.py")
        with open(caller, "w") as f:
            # The two annotation strings appear in plain comments below
            # to simulate plt.title() / plt.text() calls — the strip
            # logic removes the validate_figure(...) span, then the
            # annotations are still found in the comment lines, which
            # the modeler is free to use however they wish.
            f.write(
                "import sys\n"
                f"sys.path.insert(0, {os.path.dirname(os.path.abspath(__file__))!r})\n"
                "from figure_validator import validate_figure\n"
                "# plt.title('RMSE = 0.66pp')\n"
                "# plt.text(0.5, 0.5, 'n = 53 LGAs')\n"
                f"validate_figure({png!r}, source_data_paths=[{src!r}], "
                "expected_annotations=['RMSE = 0.66pp', 'n = 53 LGAs'])\n"
            )
        # T1: fresh PNG with annotations matching caller-script literals
        # → success, sidecar written.
        import subprocess
        env = dict(os.environ)
        result = subprocess.run([sys.executable, caller], capture_output=True,
                                text=True, env=env)
        ok(result.returncode == 0,
           f"T1: fresh PNG should pass; stderr={result.stderr}")
        ok(os.path.exists(_provenance_path(png)),
           "T1: provenance sidecar should be written")

        # T2: source CSV changes — check_staleness must report stale.
        with open(src, "w") as f:
            f.write("zone,pfpr\nNW,0.999\nNE,0.196\n")  # mutated
        status = check_staleness(png)
        ok(status["status"] == "stale" and src in status["stale_sources"],
           f"T2: mutated source should report stale; got {status}")

        # T3: annotation NOT present in caller script — must raise.
        png3 = os.path.join(d, "fig3.png")
        with open(png3, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        caller3 = os.path.join(d, "make_figure3.py")
        with open(caller3, "w") as f:
            f.write(
                "import sys\n"
                f"sys.path.insert(0, {os.path.dirname(os.path.abspath(__file__))!r})\n"
                "from figure_validator import validate_figure, FigureAnnotationMismatchError\n"
                "raised = False\n"
                "try:\n"
                f"    validate_figure({png3!r}, source_data_paths=[], "
                "expected_annotations=['THIS STRING IS NOT IN THE SCRIPT'])\n"
                "except FigureAnnotationMismatchError:\n"
                "    raised = True\n"
                "import sys\n"
                "sys.exit(0 if raised else 1)\n"
            )
        result3 = subprocess.run([sys.executable, caller3], capture_output=True,
                                 text=True, env=env)
        ok(result3.returncode == 0,
           f"T3: missing annotation should raise FigureAnnotationMismatchError; "
           f"exit={result3.returncode}, stderr={result3.stderr}")

        # T4: empty source_data_paths is allowed (soft pass — staleness
        # cannot be detected, but validate_figure must not raise).
        png4 = os.path.join(d, "fig4.png")
        with open(png4, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        caller4 = os.path.join(d, "make_figure4.py")
        with open(caller4, "w") as f:
            f.write(
                "import sys\n"
                f"sys.path.insert(0, {os.path.dirname(os.path.abspath(__file__))!r})\n"
                "from figure_validator import validate_figure\n"
                f"validate_figure({png4!r})\n"
            )
        result4 = subprocess.run([sys.executable, caller4], capture_output=True,
                                 text=True, env=env)
        ok(result4.returncode == 0,
           f"T4: empty sources + no annotations should pass; "
           f"stderr={result4.stderr}")
        ok(os.path.exists(_provenance_path(png4)),
           "T4: provenance sidecar should still be written")

        # T5: PNG missing — validate_figure must raise FileNotFoundError.
        raised = False
        try:
            validate_figure(os.path.join(d, "nope.png"))
        except FileNotFoundError:
            raised = True
        ok(raised, "T5: missing PNG should raise FileNotFoundError")

        # T6: check_staleness on a PNG with no provenance sidecar
        # returns missing_provenance (not stale).
        png6 = os.path.join(d, "fig6.png")
        with open(png6, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        status6 = check_staleness(png6)
        ok(status6["status"] == "missing_provenance",
           f"T6: no sidecar should yield missing_provenance; got {status6}")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--check-staleness", metavar="PNG_PATH",
                   help="Recompute hashes for one PNG's sidecar and report")
    args = p.parse_args()

    if args.self_test:
        return _run_self_test()
    if args.check_staleness:
        result = check_staleness(args.check_staleness)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "fresh" else 1
    p.error("use --self-test or --check-staleness <png>")
    return 2


if __name__ == "__main__":
    sys.exit(main())
