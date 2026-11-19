#!/usr/bin/env python3
"""The review workbench: a local page for verifying a v2 run (docs/design/review-workbench.md).

    python3 tools/workbench/workbench.py --roots ~/modeling-agentic-keep runs     # every run under these roots
    python3 tools/workbench/workbench.py --run-dir <run dir>                      # that run only
    python3 tools/workbench/workbench.py --retro <run dir>                        # print each check's result
    python3 tools/workbench/workbench.py --accept <run dir> <acceptance yaml>     # compare a view with quoted values
    python3 tools/workbench/workbench.py --accept <run dir> <acceptance yaml> --run-dir <other run>   # ... against that run
    python3 tools/workbench/workbench.py --self-test

Serves tools/workbench/static/ on 127.0.0.1 only and makes no outbound call. It reads run files and writes one
file: the owner's review notes, <run>/owner/review_notes.jsonl (POST /api/note, below). It imports no engine and no harness loader; every read goes through a descriptor that lib.safe_io opens with
no link followed (dir_fd_nofollow, open_nofollow), confined to the run directory as it was found at discovery.

Limit: the workbench is for the owner's own stored runs, and it does not defend against run files being replaced
while it reads them. Known places where two reads are assumed to see the same thing:
  - discovery finds a run folder by its path and its identity (device, inode) is recorded a moment later, so a
    folder swapped in between is the one that gets pinned;
  - a check that compares a file's hash with a recorded one (Run.sha) and a view that parses the same file
    (Run.json, Run.large, Run.envelope) read it separately; the one exception is the signed spec copy, which the Fit
    view hashes and parses from a single read (Run.hashed), because declarations and allowances count from it;
  - Run.large keeps a parsed file by its path, size and modification time, so a file rewritten with the same size
    and time is not parsed again;
  - a Run keeps what it read (hashes, parsed files, the artifact list) for its own lifetime, one request.
A stored run is not expected to change; a run still being written may show a mix of old and new files for a moment.

A view is a module views/<name>.py whose `view(run)` returns {checks, facts, tables, charts, notes}; the page
(static/) draws any such result and has no code for a particular view. GET /api/view?run=<id>&view=<name> returns
it (with &against=<run id>, the view compared with that other run: `choose`, below). A chart is a declaration over one of the result's tables (CHART_KEYS lists every key a view may use):
    {kind, table, title, how_to_read, x, y, low, high, group, model, model_alt, scale ("log" or absent), ...}
    bars             x: the label column, y: the value column
    lines            x and y: number columns; group: one line per value. With model, y is drawn as points and the
                     model column as the line; with low and high, a band is drawn between them
    interval_points  x: the label column; y with low and high: the observed value and its interval; model: the
                     model's value; model_alt: a second model value (hollow mark); group: rows are gathered under
                     each value
    ranges           x: the label column; outer_low, outer_high: the wide range; low, high: the narrow range
                     inside it; y: a point (each row is drawn on its own wide range, on a log scale where the
                     wide range spans a factor of 100 or more above zero, and the row says so; a point or a narrow
                     range that lies outside the wide range is drawn where it is, past the end of the wide range)
    graph            table holds one row per link, x: the column a link comes from, y: the column it goes to;
                     nodes: the table with one row per node (first column its name); group: the column, in both
                     tables, whose value marks a node or link (same, changed, missing, outside)
Keys any kind but graph takes:
    where            {column: value}: only the rows whose column has that value are drawn
    panels           a column: one chart per value of it, each on its own axis; when the panels together hold more
                     rows than fit a page, the reader picks one panel at a time
    labels           {column: words}: what a column's mark is called in the legend and on the axis (with its unit)
    limits           [{value, label}]: reference lines on the value axis (bars, interval_points, lines)
Any kind also takes:
    image            {path, hash, title, caption}: the harness figure of the same data, shown beside the chart (§3)
                     through the file endpoint below; path is the image's place in the run folder and hash its sha256;
                     title and caption are the harness's own words for the figure (optional)
    after            a table's name: the chart is drawn directly after that table, wherever the page draws the table,
                     and not in the page's list of charts. An `images` declaration takes the same key. With no such
                     table in the result, or one the run does not record, the block stays where it would have been.
A view may also return `choose`, the one control a view puts at the top of its page (CHOOSE_KEYS):
    choose           {param, label, options}: the reader picks one value for the address parameter `param` (the page
                     keeps it in its own address and sends it with the request for the view). label: the words beside
                     the control. options: a list of {value, label, group} (group optional: options are gathered under
                     it), or the word "runs", which the server replaces with the other runs it found, those whose
                     recorded question is this run's first. The server reads one such parameter, `against`: the id of
                     another run of its list, given to a view whose function takes it (`view(run, against=None)`) as
                     that run. An `against` that names no run of the list is refused (404), never answered as the run
                     alone; a view that takes the run alone is called with the run alone.
A table may carry `about` (one sentence on what its rows are and where they come from) and `labels` ({column:
words}, shown under the column's name in its header) beside columns and rows.
A table may carry `plain` (a list of columns): a status or state word in such a column is the run's own word and is
shown as written, without the mark the page gives the workbench's own status and file-state words.
A table may also carry `lead` (true: the page shows it straight after the facts, before the notes and charts,
because it is what the reader came for), `wrap` (a list of columns whose text is given room: its first four lines, and
the whole on a click, where another long text is cut to one line) and `actions`, the one way a view lets the reader write:
    actions  {post, key, label, about, fields, add}
             post    the address the page posts to; the server has one, /api/note (ACTION_POSTS)
             key     the column whose value names the row; it is sent as `row`
             label   the words on each row's button; about: one sentence shown in the form
             fields  what the form on a row asks for, a list of {name, label, choices | rows | text, required}:
                     choices is a list of words to pick from, rows (true) offers the table's other rows by their
                     key, text (true) is free text; a field left empty is not sent unless it is required
             add     {label, fields}: a form above the table that adds a row; it is sent with `row` null
A fact or column whose name ends in _usd or _minutes is shown with that unit.

Images (§5.12). Beside the five keys a view may return `images`, a list of declarations over its tables (IMAGE_KEYS):
    {table, path, hash, title, caption, group, details, flag, reason, after}
    table            the table with one row per image
    path, hash       the columns holding the image's place in the run folder and its sha256
    title, caption   the columns holding the words shown above and under the image (optional)
    group            a column: images are gathered under each value of it (optional)
    details          a list of columns shown under each image, each with its name (optional)
    flag             a column holding a status or state word: an image whose word is not an ok one is drawn first in
                     its group, and a button at the top of the block goes to it (optional)
    reason           a column saying why a row has no image: a row with no path and no hash and a reason is drawn as
                     a card with that sentence, in its group, and nothing is asked of the server for it (optional)
    after            a table's name: the block is drawn directly after that table (as for a chart, above; optional)
A caption that only repeats the image's title is not shown; one that begins with the title shows what it adds.
A note (`notes`) named after a table the run does not record (present in `tables` with the value null) is the reason
the page gives for that table under "Not recorded"; with no such note the page says the view gives no reason.
The page asks for each image at GET /api/file?run=<id>&path=<rel>&sha=<hash>. The server answers with the file's
bytes only when all of these hold, and with a reason otherwise: the Host is loopback and the method GET; the run is
one the workbench found; the name ends as a raster image the server checks does (FILE_TYPES: PNG, which is what the
harness writes; a vector file can carry a script and is refused); a view of that run folder (its path and its device
and inode as found, not its short id) that this server has sent to the page (/api/view) named that path with that
hash, in an `images` table or a chart's `image` (the last NAMED_KEPT view results are remembered, NAMED_PAIRS images
each); the path is inside the run and no credential file, read through the confined reader with no link followed and
no second hard link; the bytes read, hashed from that one read, have the hash asked for; and they have the structure
of that kind of image (png_problem: signature, header, image data, end, each chunk with its checksum, nothing after).
At most FILES_AT_ONCE images are held in memory at once, each until its answer is written (answer). The answer carries its own content policy (FILE_CSP: sandbox, nothing
allowed), not the page's, and is never stored. Answers: 200 the image, 400 no path or no hash, 403 not named or not
inside the run, 404 no such run or file, 409 the file no longer has that hash, 413 too large, 415 not a raster image
the server checks, 503 too many images being sent (ask again).

The one write (§6 "Review notes"). POST /api/note?run=<id> with a JSON body {row, kind, statement, disposition,
decides, note, same_as} appends one line to <run>/owner/review_notes.jsonl and writes nothing else, ever. It is
refused unless: the Host is loopback and any Origin is http:// followed by that same Host (name and port); the X-Workbench-Token header is the token made when the
server started (the server puts it in the page it serves, in a meta tag, so only that page has it); the body is JSON
of the schema in review_notes.py with no other key (the time is the server's; no file name comes from the client);
the run is one the workbench found; and every row the note names is in that run's register. The line is appended
with lib.safe_io.write_nofollow below a descriptor on the run folder as it was found, so an owner folder or notes
file that is a link, or a notes file also linked from elsewhere, is refused. Answers: 200 written, 400 bad note,
403 token, Host or Origin, 404 no such run, 405 any other address or method, 409 the file cannot be written safely,
413 body too long, 415 not JSON.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import hmac
import http.server
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

import yaml

# tools/, outside scripts/ and toolkit/: compute.code_fingerprint hashes those, so a workbench edit never
# invalidates a stored engine batch.
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
for _p in (os.path.join(REPO, "scripts"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from lib.safe_io import LinkRefused, dir_fd_nofollow, open_nofollow, remove_nofollow, write_nofollow  # noqa: E402
import checks  # noqa: E402
import glossary  # noqa: E402
import review_notes  # noqa: E402

STATIC = os.path.join(HERE, "static")
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/app.css": ("app.css", "text/css; charset=utf-8")}
CSP = ("default-src 'none'; script-src 'self'; connect-src 'self'; img-src 'self'; style-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
_CREDENTIAL = re.compile(r"^(?:\.env(?:\..*)?|\.netrc|id_(?:rsa|ed25519|ecdsa).*|.*\.(?:pem|key|p12)|credentials.*)$", re.I)
MAX_PARSE = 4 << 20          # a JSON or YAML file larger than this is not parsed whole by Run.json
MAX_LARGE = 64 << 20         # Run.large reads up to this much, with the C loader, and keeps the result
LARGE_KEPT = 96 << 20        # source bytes of parsed files kept in the server's memory (least recently used goes first)
_LARGE, _LARGE_LOCK, LARGE_PARSES = {}, threading.Lock(), [0]     # (run, rel) -> (size, mtime_ns, result); a count
_HASHED, HASHED_KEPT = {}, 16     # sha256 of the bytes read -> (parsed document, why not): Run.hashed
WALK_LIMIT = 20000           # files looked at when finding the last one written; a larger run folder says it was cut short
HEAD = 256 << 10             # an artifact's envelope (everything before `data:`) sits in its first bytes
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "data", "figures", "analysis", "models", "evidence_planner"}
# The views, in the order of the reader's jobs (§2): verify the run as a modeller would, then the deterministic
# steps, the register, development and spend. `goal` is the slice of §10 that builds the view.
PATH = [("overview", "Overview", "G1"), ("spec", "Spec and evidence", "G6"), ("model", "Model", "G4"), ("fit", "Fit", "G2"),
        ("scenarios", "Scenarios", "G7"), ("outcomes", "Outcomes", "G5"), ("steps", "Steps", "G1"), ("figures", "Figures", "G2b"),
        ("register", "Science register", "G3"), ("world", "World model", "G10"), ("development", "Development", "G8"),
        ("spend", "Spend", "G9")]
VIEW_FNS = {}                # name -> view function, filled on first use; a test may put one here
KEYS = {"checks": list, "facts": dict, "tables": dict, "charts": list, "notes": dict}
CHART_KINDS = ("interval_points", "ranges", "bars", "lines", "graph")
CHART_COLUMNS = ("x", "y", "low", "high", "group", "model", "model_alt", "outer_low", "outer_high", "panels")
CHART_KEYS = ("kind", "table", "title", "how_to_read", *CHART_COLUMNS, "nodes", "scale", "where", "labels", "limits", "image", "after")
IMAGE_KEYS = ("table", "path", "hash", "title", "caption", "group", "details", "flag", "reason", "after")     # a view's `images` declaration
CHART_IMAGE_KEYS = ("path", "hash", "title", "caption")                           # a chart's `image`
CHOOSE_KEYS = ("param", "label", "options")                                       # a view's `choose`
CHOOSE_PARAMS = ("against",)                  # the address parameters the server reads for a view: another run's id
OPTION_KEYS = ("value", "label", "group")
# The file endpoint (§5.12) serves raster images only: the ending a name must have, its content type and the function
# that says what in the bytes is not that kind of image (None when they are one). A vector image can carry a script,
# so it is not on the list; nor is a raster kind the harness does not write (it writes PNG) and that is not checked here.
FILE_TYPES = {".png": ("image/png", lambda data: png_problem(data))}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
FILE_CSP = "sandbox; default-src 'none'; frame-ancestors 'none'"       # an image is sent under this, not under the page's policy
MAX_FILE = 32 << 20          # an image larger than this is not served
NAMED_KEPT = 64              # view results whose named images the server remembers (the oldest goes first)
NAMED_PAIRS = 2048           # images remembered for one view result; a result that names more has the rest refused
FILES_AT_ONCE = 4            # images held in memory at once, each until its answer is written (4 x MAX_FILE at most)
FILE_WAIT = 20.0             # seconds a request for an image waits for one of those places before it is told to ask again
ACTION_KEYS = ("post", "key", "label", "about", "fields", "add")       # a table's `actions` (the module docstring)
FIELD_KEYS = ("name", "label", "choices", "rows", "text", "required")
ACTION_POSTS = ("/api/note",)                 # the addresses a page may post to: the one write the workbench has
NOTE_FIELDS = ("row", "kind", "statement", "disposition", "decides", "note", "same_as")      # what a client may send
NOTE_BODY = 32 << 10                          # bytes of a POST body read
TOKEN_MARK = "__WORKBENCH_TOKEN__"            # in static/index.html; the served page holds the server's token there
_LOOPBACK = r"(127\.0\.0\.1|localhost)(:\d+)?"


def action_problems(result):
    """What is wrong with a view result's `actions` declarations, as sentences (none when the page can draw them)."""
    out = []

    def fields(name, fs):
        if not isinstance(fs, list) or not fs:
            return [f"{name}: no list of fields"]
        bad = []
        for f in fs:
            kinds = [k for k in ("choices", "rows", "text") if isinstance(f, dict) and f.get(k)]
            if not isinstance(f, dict) or not f.get("name") or not f.get("label") or len(kinds) != 1 \
                    or any(k not in FIELD_KEYS for k in f) or ("choices" in kinds and not isinstance(f["choices"], list)):
                bad.append(f"{name}: a field is not {{name, label, one of choices | rows | text, required}} ({f})")
        return bad
    for tname, t in (result.get("tables") or {}).items():
        a = t.get("actions") if isinstance(t, dict) else None
        if a is None:
            continue
        name = f"actions of table {tname}"
        if not isinstance(a, dict):
            out.append(f"{name}: not a mapping")
            continue
        out += [f"{name}: unknown key {k!r}" for k in a if k not in ACTION_KEYS]
        if a.get("post") not in ACTION_POSTS:
            out.append(f"{name}: the server has no address {a.get('post')!r} to post to")
        if a.get("key") not in (t.get("columns") or []):
            out.append(f"{name}: table {tname} has no column {a.get('key')!r}")
        if not a.get("label"):
            out.append(f"{name}: no label")
        out += fields(name, a.get("fields"))
        if "add" in a:
            add = a["add"] if isinstance(a["add"], dict) else {}
            out += ([] if add.get("label") and set(add) <= {"label", "fields"} else [f"{name}: add is not {{label, fields}}"])
            out += fields(f"{name}, add", add.get("fields"))
    return out


def chart_problems(result):
    """What is wrong with a view result's chart declarations, as sentences (none when the page can draw them all):
    an unknown kind or key, no title or how-to-read sentence, a table or column the result does not hold."""
    out, tables = [], result.get("tables") or {}
    for ch in result.get("charts") or []:
        name = f"chart {ch.get('title')!r}"
        t = tables.get(ch.get("table"))
        cols = t.get("columns") if isinstance(t, dict) else None
        if ch.get("kind") not in CHART_KINDS:
            out.append(f"{name}: unknown kind {ch.get('kind')!r}")
        if not ch.get("title") or not ch.get("how_to_read"):
            out.append(f"{name}: no title or no how-to-read sentence")
        out += [f"{name}: unknown key {k!r}" for k in ch if k not in CHART_KEYS]
        if ch.get("after") is not None and ch["after"] not in tables:
            out.append(f"{name}: after names no table of the result ({ch['after']!r})")
        if not isinstance(cols, list):
            out.append(f"{name}: no table {ch.get('table')!r}")
            continue
        named = [ch[k] for k in CHART_COLUMNS if ch.get(k) is not None]
        named += list(ch.get("where") or {}) + list(ch.get("labels") or {})
        out += [f"{name}: table {ch['table']} has no column {c!r}" for c in named if c not in cols]
        if not all(isinstance(m, dict) and isinstance(m.get("value"), (int, float)) and m.get("label")
                   for m in ch.get("limits") or []):
            out.append(f"{name}: limits are not a list of {{value, label}}")
        im = ch.get("image")
        if im is not None and not (isinstance(im, dict) and set(im) <= set(CHART_IMAGE_KEYS) and isinstance(im.get("path"), str)
                                   and checks.digest(im.get("hash"))):
            out.append(f"{name}: image is not {{path, hash, title, caption}} with a path and a sha256")
    return out


def choose_problems(result):
    """What is wrong with a view result's `choose` declaration, as sentences (none when the page can draw it)."""
    c = result.get("choose")
    if c is None:
        return []
    if not isinstance(c, dict) or set(c) != set(CHOOSE_KEYS):
        return [f"choose is not {{{', '.join(CHOOSE_KEYS)}}}"]
    out = [] if c["param"] in CHOOSE_PARAMS else [f"choose: the server reads no parameter {c['param']!r}"]
    out += [] if isinstance(c["label"], str) and c["label"].strip() else ["choose: no label"]
    opts = c["options"]
    if opts != "runs" and not (isinstance(opts, list) and all(
            isinstance(o, dict) and set(o) <= set(OPTION_KEYS) and isinstance(o.get("value"), str) and o["value"] and isinstance(o.get("label"), str)
            for o in opts)):
        out.append('choose: options are neither "runs" nor a list of {value, label, group}')
    return out


def image_problems(result):
    """What is wrong with a view result's `images` declarations, as sentences (none when the page can draw them)."""
    out, tables = [], result.get("tables") or {}
    for d in result.get("images") or []:
        if not isinstance(d, dict):
            out.append("images: a declaration is not a mapping")
            continue
        t = tables.get(d.get("table"))
        cols = t.get("columns") if isinstance(t, dict) else None
        out += [f"images: unknown key {k!r}" for k in d if k not in IMAGE_KEYS]
        if not isinstance(cols, list):
            out.append(f"images: no table {d.get('table')!r}")
            continue
        if d.get("after") is not None and d["after"] not in tables:
            out.append(f"images: after names no table of the result ({d['after']!r})")
        named = [d.get(k) for k in ("path", "hash")] + [d[k] for k in ("title", "caption", "group", "flag", "reason") if d.get(k) is not None]
        named += d["details"] if isinstance(d.get("details"), list) else [None] if "details" in d else []
        out += [f"images: table {d['table']} has no column {c!r}" for c in named if c not in cols]
    return out


def png_problem(data):
    """What in `data` is not a PNG image, or None: the signature; then chunks, each with a length inside the file and
    the checksum of its type and body; the header chunk (IHDR, 13 bytes, a width and a height above zero) first; at
    least one chunk of image data (IDAT); the end chunk (IEND, empty) last, with nothing after it. Bytes that only
    begin as a PNG does (a signature, or a header with no image) are not an image, and bytes after the end are not
    part of one."""
    import zlib
    if not data.startswith(PNG_SIGNATURE):
        return "it does not begin as a PNG does"
    at, kinds = len(PNG_SIGNATURE), []
    while at < len(data):
        if at + 12 > len(data):
            return "it ends inside a chunk"
        size, kind = int.from_bytes(data[at:at + 4], "big"), data[at + 4:at + 8]
        end = at + 12 + size
        if end > len(data):
            return "a chunk is longer than the file"
        if zlib.crc32(data[at + 4:end - 4]) & 0xFFFFFFFF != int.from_bytes(data[end - 4:end], "big"):
            return "a chunk does not have its checksum"
        if not kinds and (kind != b"IHDR" or size != 13 or not int.from_bytes(data[at + 8:at + 12], "big")
                          or not int.from_bytes(data[at + 12:at + 16], "big")):
            return "it has no header chunk with a width and a height"
        kinds.append(kind)
        at = end
        if kind == b"IEND":
            break
    if not kinds or kinds[-1] != b"IEND" or data[at - 12:at - 8] != bytes(4):
        return "it has no end chunk"
    if at != len(data):
        return "it holds bytes after its end chunk"
    if b"IDAT" not in kinds:
        return "it holds no image data"
    return None


def named_images(result):
    """The (path, sha256) pairs a view result names: the rows of its `images` tables and its charts' `image`. Only
    these can be asked of the file endpoint (§5.12)."""
    out, tables = set(), result.get("tables") or {}
    for d in result.get("images") or []:
        t = tables.get(d.get("table")) if isinstance(d, dict) else None
        cols = t.get("columns") if isinstance(t, dict) and isinstance(t.get("rows"), list) else None
        if isinstance(cols, list) and d.get("path") in cols and d.get("hash") in cols:
            at, sha = cols.index(d["path"]), cols.index(d["hash"])
            out |= {(r[at], r[sha]) for r in t["rows"] if isinstance(r, list) and len(r) == len(cols)}
    for ch in result.get("charts") or []:
        im = ch.get("image") if isinstance(ch, dict) else None
        if isinstance(im, dict):
            out.add((im.get("path"), im.get("hash")))
    return set(sorted((p, h) for p, h in out if isinstance(p, str) and p and checks.digest(h))[:NAMED_PAIRS])


def _view_module(name):
    """The module views/<name>.py of a view that is built, or None. The registry is this list of ordinary import
    statements, one per module (no name is ever imported from data); each runs on first use, so a module that cannot
    be imported costs its own view only. A new view adds its line here (W19b holds the list to the folder)."""
    if name == "overview":
        from views import overview as m
    elif name == "fit":
        from views import fit as m
    elif name == "model":
        from views import model as m
    elif name == "figures":
        from views import figures as m
    elif name == "steps":
        from views import steps as m
    elif name == "register":
        from views import register as m
    elif name == "outcomes":
        from views import outcomes as m
    else:
        return None
    return m


def load_view(name):
    """(the function views.<name>.view, None), or (None, why): 'not built' when the registry has no such module."""
    if name not in VIEW_FNS:
        try:
            m = _view_module(name)
            if m is None:
                return None, "not built"
            VIEW_FNS[name] = m.view
        except Exception as e:      # a view that cannot be imported must not take the page down
            return None, f"views/{name}.py could not be loaded ({type(e).__name__}: {e})"
    return VIEW_FNS[name], None


def view_list():
    """The views in reading order: id, label, the goal that builds each, and whether it exists."""
    return [{"id": i, "label": lab, "goal": goal, "built": load_view(i)[1] != "not built"} for i, lab, goal in PATH]


def unavailable(name, says):
    """The result every view gives when it has nothing to show: one `not_available` check with the reason."""
    label = next((lab for i, lab, _ in PATH if i == name), name)
    return {"checks": [checks.result(f"{name}.view", name, f"The {label} view", "not_available", says)],
            "facts": {}, "tables": {}, "charts": [], "notes": {}}


def takes_against(fn):
    """True when a view's function takes the comparison run (`view(run, against=None)`)."""
    import inspect
    try:
        return "against" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def run_view(run, name, against=None):
    """One view's result for one run, always in the {checks, facts, tables, charts, notes} shape; a view's `images`
    declarations (a list) and its `choose` declaration (a mapping the page can draw) are passed through beside them
    when it gives any. `against`: the comparison run, given only to a view whose function takes one."""
    fn, why = load_view(name)
    if fn is None:
        goal = next((g for i, _, g in PATH if i == name), None)
        return unavailable(name, f"This view is not built yet; goal {goal} builds it." if why == "not built" else why)
    try:
        out = fn(run, against=against) if against is not None and takes_against(fn) else fn(run)
    except Exception as e:
        return unavailable(name, f"This view failed on the run's files ({type(e).__name__}: {e}).")
    if not isinstance(out, dict) or any(not isinstance(out.get(k, t()), t) for k, t in KEYS.items()):
        return unavailable(name, "This view returned something other than checks, facts, tables, charts and notes.")
    return {**{k: out.get(k, t()) for k, t in KEYS.items()}, **({"images": out["images"]} if isinstance(out.get("images"), list) else {}),
            **({"choose": out["choose"]} if isinstance(out.get("choose"), dict) and not choose_problems(out) else {})}


def clean(x):
    """JSON the browser can parse: a number that is not finite becomes null, and an unknown type its text."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    return x if isinstance(x, (str, int, bool, type(None))) else str(x)


def check_rel(rel):
    rel = str(rel or "")
    parts = rel.replace("\\", "/").split("/")
    if not rel or os.path.isabs(rel) or "\0" in rel or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"{rel[:120]!r}: not a relative path inside the run directory")
    if any(_CREDENTIAL.match(x) for x in parts):
        raise ValueError(f"{rel[:120]}: credential files are not read")
    return rel


def _ts(s):
    try:
        t = _dt.datetime.fromisoformat(str(s))
        return t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def ago(seconds):
    """A length of time in plain words: less than a minute, 5 minutes, 2 hours, 3 days."""
    for size, word in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {word}{'' if n == 1 else 's'}"
    return "less than a minute"


def folder_id(path):
    """(device, inode) of a directory, itself and not what a link at that path points to; (None, None) when it is
    not a directory or cannot be read. Recorded when a run is found, and compared on every later read."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return None, None
    return (st.st_dev, st.st_ino) if stat.S_ISDIR(st.st_mode) else (None, None)


class Run:
    """One run directory, read lazily. Every reader returns (value, why_not): never raises on a bad file.

    Every read goes through a descriptor: the run folder is opened with no link followed and must still be the
    directory found at discovery (`ident`, its device and inode: a folder swapped for another, or for a link, is
    refused); the file is opened below that descriptor with no link followed at any component; and a file with more
    than one hard link is refused, since its content may belong to a file elsewhere."""

    def __init__(self, path, repo=REPO, now=None, ident=None):
        # With no identity given, this is the discovery: the path is resolved once, here, and its identity recorded.
        self.path = os.path.realpath(path) if ident is None else path
        self.ident = folder_id(self.path) if ident is None else tuple(ident)
        self.repo, self.now, self._cache = repo, now, {}
        self.label = None        # the server's short name for the run, set where one run is shown beside another

    def _root(self):
        """A descriptor on the run folder (the caller closes it); LinkRefused when it is not the folder found."""
        fd = dir_fd_nofollow(self.path, [])
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != self.ident:
            os.close(fd)
            raise LinkRefused("the run folder is not the directory found at discovery (its device and inode differ), "
                              "so nothing is read from it")
        return fd

    def _dir(self, parts):
        """A descriptor on a folder of the run, each component opened with no link followed (the caller closes it)."""
        root = self._root()
        try:
            return dir_fd_nofollow(root, list(parts))
        finally:
            os.close(root)

    def _open(self, rel, fn):
        """(fn(descriptor, its fstat), None) for the plain file `rel`, or (None, why)."""
        try:
            root = self._root()
            try:
                fd = open_nofollow(root, check_rel(rel))
            finally:
                os.close(root)
        except FileNotFoundError:
            return None, f"{rel} is not in the run folder"
        except (ValueError, OSError) as e:
            return None, f"{rel} could not be read ({e})"
        try:
            st = os.fstat(fd)
            if st.st_nlink > 1:
                return None, (f"{rel} could not be read (it has {st.st_nlink} hard links: a file also linked from "
                              f"elsewhere is not read)")
            return fn(fd, st), None
        except OSError as e:
            return None, f"{rel} could not be read ({e})"
        finally:
            os.close(fd)

    @staticmethod
    def _pieces(fd, max_bytes=None):
        left = -1 if max_bytes is None else max_bytes
        while left != 0:
            b = os.read(fd, 1 << 20 if left < 0 else min(left, 1 << 20))
            if not b:
                return
            left -= len(b) if left > 0 else 0
            yield b

    def names(self, rel):
        """The names in a folder of the run, listed through a descriptor opened with no link followed; none when it
        cannot be opened that way (a folder that is a link is not listed)."""
        try:
            fd = self._dir(check_rel(rel).split("/") if rel else [])      # "" is the run folder itself
        except (ValueError, OSError):
            return []
        try:
            return sorted(os.listdir(fd))
        except OSError:
            return []
        finally:
            os.close(fd)

    def last_written(self, limit=WALK_LIMIT):
        """{at, file, cut}: the newest modification time among the run's files and the file that has it, from a walk
        of the run folder that follows no link and stops after `limit` files (cut is then True). Each folder is
        opened by descriptor, component by component, so one replaced by a link during the walk is not entered. Only
        file times are read, never contents. at is None when no file's time could be read."""
        key = ("last_written", limit)
        if key not in self._cache:
            at, rel, n, cut, stack = None, None, 0, False, [()]
            while stack and not cut:
                parts = stack.pop()
                try:
                    fd = self._dir(parts)
                except (ValueError, OSError):
                    continue
                try:
                    with os.scandir(fd) as it:
                        entries = sorted(it, key=lambda e: e.name)
                    for e in entries:
                        try:
                            if e.is_symlink():
                                continue
                            if e.is_dir(follow_symlinks=False):
                                stack.append(parts + (e.name,))
                                continue
                            n += 1
                            if n > limit:
                                cut = True
                                break
                            t = e.stat(follow_symlinks=False).st_mtime
                        except OSError:
                            continue
                        if at is None or t > at:
                            at, rel = t, "/".join(parts + (e.name,))
                except OSError:
                    continue
                finally:
                    os.close(fd)
            self._cache[key] = {"at": at, "file": rel, "cut": cut}
        return self._cache[key]

    def written(self):
        """The last file written, for a sentence: {at, file, ago (plain words), utc, says}, or says alone when no
        file's time could be read."""
        lw = self.last_written()
        if lw["at"] is None:
            return {**lw, "ago": None, "utc": None, "says": "No file's modification time could be read in the run folder."}
        words = ago(max(0.0, (time.time() if self.now is None else self.now) - lw["at"]))
        utc = _dt.datetime.fromtimestamp(lw["at"], _dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        among = f" among the first {WALK_LIMIT:,} files looked at" if lw["cut"] else ""
        return {**lw, "ago": words, "utc": utc,
                "says": f"The newest file in the run folder{among}, {lw['file']}, was written {words} ago ({utc})."}

    def read(self, rel, max_bytes=None):
        return self._open(rel, lambda fd, st: b"".join(self._pieces(fd, max_bytes)))

    def lines(self, rel, fn):
        """([fn(line number, the line's bytes) for every line that is not blank, where fn gives something], None), or
        (None, why). The file is read to its end piece by piece and never cut at a size: a row cannot be lost
        beyond a read limit. Only what `fn` returns is kept."""
        def read(fd, st):
            out, tail, n = [], b"", 0
            for piece in self._pieces(fd):
                parts = (tail + piece).split(b"\n")
                tail = parts.pop()
                for raw in parts:
                    n += 1
                    got = fn(n, raw) if raw.strip() else None
                    if got is not None:
                        out.append(got)
            got = fn(n + 1, tail) if tail.strip() else None
            return out + ([got] if got is not None else [])
        return self._open(rel, read)

    def sha(self, rel):
        """The file's sha256, hashed piece by piece from the confined descriptor (a file of any size costs one
        piece of memory); None when it cannot be read."""
        key = ("sha", rel)
        if key not in self._cache:
            def digest(fd, st):
                h = hashlib.sha256()
                for b in self._pieces(fd):
                    h.update(b)
                return h.hexdigest()
            self._cache[key] = self._open(rel, digest)[0]
        return self._cache[key]

    def json(self, rel):
        """A JSON or YAML mapping, parsed whole (files up to MAX_PARSE)."""
        key = ("doc", rel)
        if key not in self._cache:
            b, why = self.read(rel, MAX_PARSE + 1)
            if b is None:
                self._cache[key] = (None, why)
            elif len(b) > MAX_PARSE:
                self._cache[key] = (None, f"{rel} is larger than {MAX_PARSE >> 20} MB and is not read whole")
            else:
                try:
                    d = json.loads(b) if rel.endswith(".json") else yaml.safe_load(b)
                    self._cache[key] = (d, None) if isinstance(d, dict) else (None, f"{rel} is not a mapping")
                except (ValueError, yaml.YAMLError) as e:
                    self._cache[key] = (None, f"{rel} could not be parsed ({str(e)[:120]})")
        return self._cache[key]

    def large(self, rel):
        """A YAML mapping of any size up to MAX_LARGE, parsed with PyYAML's C loader and kept in the server's memory
        by the file's size and modification time: every later request, from any Run of this folder, gets the parsed
        document back until the file changes. The stat and the read are of one descriptor, opened by the confined,
        no-link reader. The caller must not change the document it is given."""
        out, why = self._open(rel, lambda fd, st: self._large(rel, fd, st))
        return out if why is None else (None, why)

    def _large(self, rel, fd, st):
        key = (self.path, rel)
        with _LARGE_LOCK:        # one parse at a time: a second request for the same file waits and then finds it
            kept = _LARGE.get(key)
            if kept and kept[:2] == (st.st_size, st.st_mtime_ns):
                _LARGE[key] = _LARGE.pop(key)
                return kept[2]
            if st.st_size > MAX_LARGE:
                return None, f"{rel} is larger than {MAX_LARGE >> 20} MB and is not read"
            b = b"".join(self._pieces(fd, MAX_LARGE + 1))
            try:
                LARGE_PARSES[0] += 1
                d = yaml.load(b, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
                out = (d, None) if isinstance(d, dict) else (None, f"{rel} is not a mapping")
            except (ValueError, yaml.YAMLError) as e:
                out = (None, f"{rel} could not be parsed ({' '.join(str(e).split())[:120]})")
            _LARGE.pop(key, None)
            _LARGE[key] = (st.st_size, st.st_mtime_ns, out)
            while len(_LARGE) > 1 and sum(v[0] for v in _LARGE.values()) > LARGE_KEPT:
                _LARGE.pop(next(iter(_LARGE)))
            return out

    def hashed(self, rel):
        """(sha256, parsed YAML mapping or None, why_not) of a file from ONE read: the bytes that are hashed are the
        bytes that are parsed, so a hash that verifies speaks for the document returned. A parse is kept by its
        digest alone (never by path, size or time), so it can only be given back for the same bytes. sha256 is None
        when the file cannot be read or is larger than MAX_LARGE. The caller must not change the document."""
        b, why = self.read(rel, MAX_LARGE + 1)
        if b is None:
            return None, None, why
        if len(b) > MAX_LARGE:
            return None, None, f"{rel} is larger than {MAX_LARGE >> 20} MB and is not read"
        sha = hashlib.sha256(b).hexdigest()
        with _LARGE_LOCK:
            if sha not in _HASHED:
                try:
                    d = yaml.load(b, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
                    _HASHED[sha] = (d, None) if isinstance(d, dict) else (None, "it is not a mapping")
                except (ValueError, yaml.YAMLError) as e:
                    _HASHED[sha] = (None, f"it could not be parsed ({' '.join(str(e).split())[:120]})")
                while len(_HASHED) > HASHED_KEPT:
                    _HASHED.pop(next(iter(_HASHED)))
            d, bad = _HASHED[sha]
        return sha, d, (f"{rel}: {bad}" if bad else None)

    def envelope(self, rel):
        """A harness artifact's header (schema, producer, spec_sha256, inputs, written_at): the YAML before its
        top-level `data:` key, read from the file's first bytes so a 13 MB artifact costs the same as a small one."""
        b, why = self.read(rel, HEAD)
        if b is None:
            return None, why
        head = re.split(rb"(?m)^data:", b, maxsplit=1)[0]
        try:
            d = yaml.safe_load(head)
        except yaml.YAMLError:
            d = None
        return (d, None) if isinstance(d, dict) else (None, f"{rel} has no readable header")

    def commit_known(self, commit):
        """True/False when git can say whether `commit` is in this checkout; None when git cannot be asked."""
        if not commit or not re.fullmatch(r"[0-9a-f]{7,40}", str(commit)):
            return None
        try:
            r = subprocess.run(["git", "-C", self.repo, "cat-file", "-e", f"{commit}^{{commit}}"],
                               capture_output=True, timeout=10)
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return None

    def artifacts(self):
        """Every harness-written analysis file with its header and, per recorded input, whether the file on disk
        still has the hash it had when the artifact was computed."""
        if "artifacts" in self._cache:
            return self._cache["artifacts"]
        man, _ = self.json("analysis/manifest.json")
        listed = dict((man or {}).get("artifacts") or {})
        for name in self.names("analysis"):
            if name.endswith(".yaml"):
                listed.setdefault(f"analysis/{name}", {})
        out = []
        for rel, rec in sorted(listed.items()):
            env, why = self.envelope(rel) if rel.endswith(".yaml") else (None, "not a YAML artifact")
            env = env or {}
            inputs = []
            for ipath, isha in sorted((env.get("inputs") or {}).items() if isinstance(env.get("inputs"), dict) else []):
                if str(ipath).startswith(".."):
                    now, state = None, "outside"       # a harness file (engine card, priors): not in the run folder
                else:
                    now = self.sha(ipath)
                    state = checks.hash_state(isha, now)
                inputs.append({"path": ipath, "sha256": isha, "now": now, "state": state})
            eng = env.get("engine") if isinstance(env.get("engine"), dict) else {}
            out.append({"path": rel, "kind": (rec or {}).get("kind") or str(env.get("schema") or "").split("/")[0],
                        "producer": env.get("producer") or (rec or {}).get("producer"),
                        "written_at": env.get("written_at"), "spec_sha256": env.get("spec_sha256"),
                        "spec_signed": env.get("spec_signed"), "engine": " ".join(
                            str(eng.get(k)) for k in ("name", "version") if eng.get(k)) or None,
                        "sha256": self.sha(rel), "in_manifest": rel in ((man or {}).get("artifacts") or {}),
                        "inputs": inputs, "why": why if not env else None})
        self._cache["artifacts"] = out
        return out

    def steps(self):
        """The stage history with time and model spend per transition, and the task journal."""
        state, why = self.json("run_state.json")
        if state is None:
            return {"why": why, "history": [], "tasks": []}
        hist, prev_t, prev_usd = [], None, 0.0
        meta, _ = self.json("metadata.json")
        prev_t = _ts((meta or {}).get("started"))
        for h in state.get("history") or []:
            if not isinstance(h, dict) or "from" not in h or "to" not in h:
                continue                # other entries (a fix loop's summary) are not transitions
            t, usd = _ts(h.get("at")), h.get("spent_usd")
            usd = float(usd) if isinstance(usd, (int, float)) else None
            mins = round((t - prev_t).total_seconds() / 60, 1) if t and prev_t else None
            hist.append({"at": h.get("at"), "from": str(h.get("from")), "to": str(h.get("to")), "why": str(h.get("why") or ""),
                         "round": h.get("round"), "spent_usd": usd, "minutes": mins,
                         "usd": round(usd - prev_usd, 4) if usd is not None else None})
            prev_t, prev_usd = t or prev_t, usd if usd is not None else prev_usd
        tasks = []
        for key, j in (state.get("journal") or {}).items():
            if not isinstance(j, dict):
                continue
            a, b = _ts(j.get("started")), _ts(j.get("completed"))
            tasks.append({"key": key, "stage": key.split("#")[0], "task": j.get("task"), "status": j.get("status"),
                          "ok": j.get("ok"), "started": j.get("started"),
                          "minutes": round((b - a).total_seconds() / 60, 1) if a and b else None,
                          "usd": j.get("usd") if isinstance(j.get("usd"), (int, float)) else None,
                          "allowance_usd": j.get("allowance_usd") if isinstance(j.get("allowance_usd"), (int, float)) else None,
                          "note": str(j.get("note") or (j.get("output") or {}).get("notes") or "")
                          if isinstance(j.get("output"), (dict, type(None))) else str(j.get("note") or "")})
        return {"why": None, "history": hist, "tasks": tasks, "state": state.get("state"), "status": state.get("status"),
                "paused": state.get("paused"), "round": state.get("round"), "max_rounds": state.get("max_rounds"),
                "budget_usd": state.get("budget_usd"), "spent_usd": state.get("spent_usd")}

    def summary(self):
        state, why = self.json("run_state.json")
        meta, _ = self.json("metadata.json")
        state, meta = state or {}, meta or {}
        q = str(state.get("question") or meta.get("question") or "")
        w = self.written()
        return {"question": q[:400], "state": state.get("state"), "status": state.get("status") or meta.get("status"),
                "paused": state.get("paused"), "pipeline": meta.get("pipeline") or "unknown",
                "commit": meta.get("commit"), "launcher": meta.get("launcher"), "started": meta.get("started"),
                "finished": meta.get("finished"), "spent_usd": state.get("spent_usd"), "engine": meta.get("engine"),
                "round": state.get("round"), "last_written": w["at"], "last_written_file": w["file"],
                "last_written_ago": w["ago"], "last_written_utc": w["utc"], "why": why}

    def view(self, name):
        """A view's result, computed once per Run (the overview asks for every other view)."""
        if ("view", name) not in self._cache:
            self._cache[("view", name)] = run_view(self, name)
        return self._cache[("view", name)]

    views = staticmethod(lambda: view_list())


def discover(roots, max_depth=9):
    """Every directory under the roots that holds a run_state.json."""
    found = []
    for root in roots:
        root = os.path.realpath(os.path.expanduser(root))
        for d, dirs, files in os.walk(root):
            depth = d[len(root):].count(os.sep)
            if "run_state.json" in files:
                found.append(d)
                dirs[:] = []
                continue
            dirs[:] = [] if depth >= max_depth else [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
    return sorted(set(found))


def chosen_roots(roots, run_dirs):
    """The folders to search for runs: those given; else none when run folders are named one by one; else runs/."""
    return list(roots) if roots is not None else ([] if run_dirs else [os.path.join(REPO, "runs")])


class Workbench:
    def __init__(self, roots, run_dirs=(), spec=None):
        self.roots, self.run_dirs, self.runs, self.lock = list(roots), [os.path.realpath(p) for p in run_dirs], {}, threading.Lock()
        self.found = {}                          # path -> (device, inode) of the folder when it was first found
        self.token = secrets.token_urlsafe(32)   # made at start; only the page this server serves holds it
        self.writing = threading.Lock()          # one note is appended at a time
        # (run folder, its device and inode, view) -> the (path, sha256) pairs that view result named. The folder as it
        # was found, never the run's short id: an id that comes to lead elsewhere carries no permission with it.
        self.named = {}
        self.file_slots = threading.BoundedSemaphore(FILES_AT_ONCE)       # held by answer() from the read to the write
        self.file_wait = FILE_WAIT
        self.glossary = glossary.load(spec)      # read once at start from the specification's own tables

    def scan(self):
        paths = self.run_dirs + discover(self.roots)
        with self.lock:
            for p in paths:
                self.found.setdefault(p, folder_id(p))
            self.runs = {hashlib.sha1(p.encode()).hexdigest()[:10]: p for p in paths}
        return self.runs

    def run(self, path):
        """The run at a path the scan found, held to the folder it was then (Run refuses another in its place)."""
        return Run(path, ident=self.found.get(path, (None, None)))

    def label(self, path):
        """The run's place under its root, with the long question-derived folder name shortened."""
        for root in [os.path.realpath(os.path.expanduser(r)) for r in self.roots]:
            if path.startswith(root + os.sep):
                path = path[len(root) + 1:]
                break
        parts = [p for p in path.split(os.sep) if p not in ("out", "runs", "work")]
        if parts and re.match(r"_?\d{4}-\d\d-\d\d_\d+_", parts[-1]):
            tail = re.sub(r"^(_?\d{4}-\d\d-\d\d)_\d+_.{0,40}?(-[a-z]\d+)?(_.*)?$", r"\1\2\3", parts[-1])
            parts[-1] = tail
        return " / ".join(parts[-4:])

    def api_runs(self):
        rows = []
        for rid, p in self.scan().items():
            s = self.run(p).summary()
            rows.append({"id": rid, "label": self.label(p), "dir": p, **s,
                         "copy": bool(re.search(r"(before|backup|pristine|\.bak)", p, re.I))})
        rows.sort(key=lambda r: -(r["last_written"] or 0))
        return {"runs": rows, "roots": self.roots, "run_dirs": self.run_dirs, "views": view_list()}

    def others(self, path):
        """The other runs of the list as a `choose` control's options: those whose recorded question is this run's
        first, each group by label. The question is the one the run's state records, a hint for the reader; a view
        that compares two runs holds them to their signed specs itself."""
        ask = " ".join(str(self.run(path).summary()["question"]).split())
        rows = []
        for rid, p in self.scan().items():
            if p != path:
                same = bool(ask) and " ".join(str(self.run(p).summary()["question"]).split()) == ask
                rows.append((not same, self.label(p), {"value": rid, "label": self.label(p),
                                                       "group": "the same recorded question" if same else "another recorded question"}))
        return [o for _, _, o in sorted(rows, key=lambda r: r[:2])]

    def api_view(self, rid, name, against=None):
        """The data endpoint (§9): one view of one run, or None when there is no such run or view. `against`: the id
        of the comparison run, which must be another run of the list (the caller refuses one that is not)."""
        with self.lock:
            p, q = self.runs.get(rid), self.runs.get(against) if against else None
        p = p or self.scan().get(rid)
        if p is None or name not in [i for i, _, _ in PATH]:
            return None
        run = self.run(p)
        if q is not None:       # never kept as the answer for the run alone (Run.view keeps one result per view)
            other = self.run(q)
            other.label = self.label(q)
            out = clean(run_view(run, name, other))
        else:
            out = clean(run.view(name))
        if isinstance(out.get("choose"), dict) and out["choose"].get("options") == "runs":
            out["choose"] = {**out["choose"], "options": self.others(p)}
        with self.lock:      # what this answer names is what the page may ask the file endpoint for, in this folder only
            self.named.pop((run.path, run.ident, name), None)
            self.named[(run.path, run.ident, name)] = named_images(out)
            while len(self.named) > NAMED_KEPT:
                self.named.pop(next(iter(self.named)))
        return out

    def api_file(self, rid, rel, sha):
        """The file endpoint (§5.12): (status, content type, body, headers) for one image of one run. The rules are
        in the module docstring; every refusal says why and holds none of the file's bytes."""
        def no(code, why):
            return code, "text/plain; charset=utf-8", f"not served: {why}".encode(), {}
        with self.lock:
            p = self.runs.get(rid)
        p = p or self.scan().get(rid)
        if p is None:
            return no(404, "no such run")
        if not rel or checks.digest(sha) is None:
            return no(400, "the request must give a path and a sha256 (path=, sha=)")
        kind = FILE_TYPES.get(os.path.splitext(rel)[1].lower())
        if kind is None:
            return no(415, f"only raster images are served ({', '.join(FILE_TYPES)})")
        run = self.run(p)        # the folder the id leads to now: its path and the identity recorded when it was found
        with self.lock:
            named = any((rel, sha) in pairs for (path, ident, _), pairs in self.named.items() if (path, ident) == (run.path, run.ident))
        if not named:
            return no(403, "no view of this run that the page has been sent names this path with this hash")
        try:
            check_rel(rel)
        except ValueError as e:
            return no(403, str(e))
        data, why = run.read(rel, MAX_FILE + 1)      # that same folder: confined, no link followed, no second hard link
        if data is None:
            return no(404 if str(why).endswith("is not in the run folder") else 403, why)
        if len(data) > MAX_FILE:
            return no(413, f"{rel} is larger than {MAX_FILE >> 20} MB")
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), sha):       # the bytes read are the bytes hashed and sent
            return no(409, f"{rel} no longer has the hash the view named: read the files again")
        bad = kind[1](data)
        if bad:
            return no(415, f"{rel} is not an image of its kind: {bad}")
        return 200, kind[0], data, {"Content-Security-Policy": FILE_CSP, "Content-Disposition": "inline",
                                    "Cross-Origin-Resource-Policy": "same-origin"}

    def add_note(self, rid, body):
        """(status, answer) for one review note posted for the run `rid`: checked against the schema and the run's
        register, then appended as one line to <run>/owner/review_notes.jsonl. Nothing else is written."""
        try:
            doc = json.loads(body.decode("utf-8"))
        except ValueError:
            return 400, {"error": "the body is not JSON"}
        if not isinstance(doc, dict) or any(k not in NOTE_FIELDS for k in doc):
            return 400, {"error": f"the body is not an object with only the keys {', '.join(NOTE_FIELDS)}"}
        note = {"at": review_notes.now(), **{k: doc.get(k) for k in review_notes.KEYS[1:]}}
        note["note"] = "" if note["note"] is None else note["note"]
        if doc.get("same_as") is not None:
            note["same_as"] = doc["same_as"]
        note, why = review_notes.check(note)
        if note is None:
            return 400, {"error": f"the note is not written: {why}"}
        with self.lock:
            p = self.runs.get(rid)
        p = p or self.scan().get(rid)
        if p is None:
            return 404, {"error": "no such run"}
        with self.writing:
            run = self.run(p)
            rows = run.view("register")["tables"].get("rows") or {}
            at = rows.get("columns", []).index("id") if "id" in rows.get("columns", []) else None
            held = {r[at] for r in rows.get("rows", [])} if at is not None else set()
            for k in ("row", "same_as"):
                if note.get(k) is not None and note[k] not in held:
                    return 400, {"error": f"the note is not written: the register holds no row {note[k]!r}"}
            existing, why = run.read(review_notes.FILE, review_notes.MAX_FILE + 1)
            if existing is None and not str(why).endswith("is not in the run folder"):
                return 409, {"error": f"the note is not written: {why}"}
            try:
                root = run._root()
                try:
                    review_notes.append(root, note, existing)
                finally:
                    os.close(root)
            except (ValueError, OSError) as e:       # a link at the folder or the file, a file linked from elsewhere
                return 409, {"error": f"the note is not written: {e}"}
        return 200, {"ok": True, "file": review_notes.FILE, "at": note["at"]}

    def handle(self, method, target, host, body=b"", headers=None):
        """(status, content type, body), and for /api/file a fourth element: the headers to send in place of the usual
        ones ({name: value}; an image has its own content policy). Loopback Host only. GET reads: /api/runs lists the
        runs, /api/view gives one view of one run, /api/file one image a view named (api_file), /api/glossary the
        meanings of the codes the page shows (glossary.py reads them from the specification). The one write is POST /api/note (the module docstring says what it refuses); `headers` holds
        the request's headers by lower-case name."""
        if not re.fullmatch(_LOOPBACK, host or ""):
            return 403, "text/plain", b"loopback only"
        u = urllib.parse.urlsplit(target)
        q = urllib.parse.parse_qs(u.query)
        if method == "POST" and u.path in ACTION_POSTS:
            h = headers or {}
            origin = h.get("origin")
            # The server speaks http only, and the page that may post is the one it served: an Origin, when a browser
            # sends one, must be this scheme and the request's own authority (name and port), not merely loopback.
            if origin is not None and origin != f"http://{host}":
                return 403, "text/plain", b"this page only: the Origin is not this server's address"
            if not hmac.compare_digest(str(h.get("x-workbench-token") or "").encode(), self.token.encode()):
                return 403, "text/plain", b"the token is not this server's: reload the page"
            if str(h.get("content-type") or "").split(";")[0].strip().lower() != "application/json":
                return 415, "text/plain", b"the body must be application/json"
            if len(body or b"") > NOTE_BODY:
                return 413, "text/plain", b"the body is too long"
            code, doc = self.add_note((q.get("run") or [""])[0], body or b"")
            return code, "application/json", json.dumps(doc).encode()
        if method != "GET":
            return 405, "text/plain", b"read only, but for POST /api/note"
        if u.path in STATIC_FILES:
            name, ctype = STATIC_FILES[u.path]
            with open(os.path.join(STATIC, name), "rb") as f:
                data = f.read()
            return 200, ctype, data.replace(TOKEN_MARK.encode(), self.token.encode()) if name == "index.html" else data
        if u.path == "/api/runs":
            return 200, "application/json", json.dumps(clean(self.api_runs())).encode()
        if u.path == "/api/glossary":
            return 200, "application/json", json.dumps(clean(self.glossary)).encode()
        if u.path == "/api/view":
            rid, against = (q.get("run") or [""])[0], (q.get("against") or [None])[0]
            if against is not None:
                with self.lock:
                    known = against in self.runs
                if not (known or against in self.scan()) or against == rid:
                    return 404, "text/plain", (b"no such comparison run: `against` must be the id of another run of this server's list, "
                                               b"so nothing is compared")
            d = self.api_view(rid, (q.get("view") or [""])[0], against)
            return (200, "application/json", json.dumps(d).encode()) if d else (404, "text/plain", b"no such run or view")
        if u.path == "/api/file":
            return self.api_file((q.get("run") or [""])[0], (q.get("path") or [""])[0], (q.get("sha") or [""])[0])
        return 404, "text/plain", b"not found"


def response_headers(ctype, length, more=None):
    """The headers of every answer; `more` (what Workbench.handle gives for a file) replaces those of the same name,
    so an image goes out under its own content policy. Nothing the server sends is stored by the browser."""
    out = {"Content-Type": ctype, "Content-Length": str(length), "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
           "Referrer-Policy": "no-referrer", "Content-Security-Policy": CSP}
    return {**out, **(more or {})}


def answer(wb, method, target, host, body, headers, send):
    """One request answered through `send(status, headers, body)`, which writes to the socket. An image is held in
    memory from its read until `send` returns, so a request for one takes one of FILES_AT_ONCE places first and keeps
    it through the write; when none comes free within wb.file_wait seconds it is told to ask again (503), holding
    nothing. So slow readers cannot make the server hold more than FILES_AT_ONCE images at once."""
    is_file = urllib.parse.urlsplit(target).path == "/api/file"
    held = wb.file_slots.acquire(timeout=wb.file_wait) if is_file else False
    try:
        if is_file and not held:
            code, ctype, out, more = 503, "text/plain; charset=utf-8", b"not served: the server is sending other images; ask again", [{"Retry-After": "1"}]
        else:
            try:
                code, ctype, out, *more = wb.handle(method, target, host, body, headers)
            except Exception as e:      # the page shows the reason; the server stays up
                code, ctype, out, more = 500, "text/plain", f"{type(e).__name__}: {e}".encode(), []
        send(code, response_headers(ctype, len(out), more[0] if more else None), out)
    finally:
        if held:
            wb.file_slots.release()


def serve(wb, port):
    class H(http.server.BaseHTTPRequestHandler):
        def _do(self):
            size = self.headers.get("Content-Length") or "0"
            size = int(size) if size.isdigit() else 0
            sent = self.rfile.read(min(size, NOTE_BODY + 1)) if self.command == "POST" else b""

            def send(code, headers, body):
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
            answer(wb, self.command, self.path, self.headers.get("Host"), sent, {k.lower(): v for k, v in self.headers.items()}, send)
        do_GET = do_POST = do_PUT = do_DELETE = _do

        def log_message(self, *a):
            pass
    for p in ([port] if port else range(8790, 8810)):
        try:
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", p), H)
            break
        except OSError:
            if port:
                raise
    else:
        raise OSError("no free port in 8790-8809")
    print(f"review workbench: http://127.0.0.1:{srv.server_address[1]}/  ({len(wb.scan())} runs)", flush=True)
    srv.serve_forever()


def retro(run_dir):
    """Print each check's result for one run: the loop's evidence that a view catches what it claims to."""
    cs = Run(run_dir).view("overview")["checks"]
    for c in cs:
        print(f"[{c['status']:^13}] {c['id']:<22} {c['case'] or '':<4} {c['says']}")
    return 0 if cs else 1


def lookup(doc, path):
    """The value at `path` in a view's result: facts.x, tables.t[col=value].col2, checks[id=x].status.
    `[col=value]` picks the first row (of a table or a list) whose `col` reads as `value`."""
    cur, pos = doc, 0
    for m in re.finditer(r"([^.\[\]]+)(?:\[([^=\]]+)=([^\]]*)\])?(?:\.|$)", path):
        if m.start() != pos:
            break
        pos, (name, col, want) = m.end(), m.groups()
        if not isinstance(cur, dict) or name not in cur:
            raise LookupError(f"no '{name}'")
        cur = cur[name]
        if col is not None:
            if isinstance(cur, dict) and isinstance(cur.get("columns"), list):
                cur = [dict(zip(cur["columns"], r)) for r in cur.get("rows") or []]
            rows = [r for r in cur if isinstance(r, dict) and str(r.get(col)) == want] if isinstance(cur, list) else []
            if not rows:
                raise LookupError(f"no row of '{name}' has {col} = {want}")
            cur = rows[0]
    if pos != len(path) or not path:
        raise LookupError("the path is not of the form a.b[col=value].c")
    return cur


def accept(wb, run_dir, path, out=print):
    """Compare the data endpoint's answer for one run with an acceptance file (§9): a list of
    {path, equals | approx, source}, bare or under `expect:` beside `run:` and `view:`
    (the view is `view:`, else the file's name). One line per entry; 1 on any difference.
    A top-level `against:` names the comparison run by its path (whole, or its end under the keep folder): the view is
    asked for with that run as `&against=`, and the run must be one the workbench was given (`--run-dir`)."""
    name, against = os.path.splitext(os.path.basename(path))[0], None
    try:
        with open(path) as f:
            entries = yaml.safe_load(f)
        if isinstance(entries, dict):      # the full form: {run, view, against, expect: [...]}
            name, against, entries = str(entries.get("view") or name), entries.get("against"), entries.get("expect")
        assert isinstance(entries, list) and all(isinstance(e, dict) for e in entries), "it is not a list of entries"
        assert against is None or (isinstance(against, str) and against.strip("/")), "`against` is not a run's path"
    except (OSError, yaml.YAMLError, AssertionError) as e:
        out(f"DIFF  {path}: the acceptance file could not be read ({e})")
        return 1
    runs = wb.scan()
    rid = next((k for k, p in runs.items() if p == os.path.realpath(run_dir)), "")
    more = ""
    if against is not None:
        end = os.sep + against.strip("/").replace("/", os.sep)
        other = [k for k, p in runs.items() if k != rid and (p == os.path.realpath(os.path.expanduser(against)) or p.endswith(end))]
        if len(other) != 1:
            out(f"DIFF  {path}: `against` ({against}) names {len(other)} of the runs given; give that run with --run-dir")
            return 1
        more = f"&against={other[0]}"
    code, _, body = wb.handle("GET", f"/api/view?run={rid}&view={urllib.parse.quote(name)}{more}", "127.0.0.1")
    doc, bad = (json.loads(body) if code == 200 else None), 0
    for e in entries:
        p, src = str(e.get("path")), f"  [{e.get('source')}]" if e.get("source") else ""
        kind = next((k for k in ("equals", "approx") if k in e), None)
        try:
            if doc is None:
                raise LookupError(f"the endpoint has no view '{name}' for this run ({code})")
            if kind is None:
                raise LookupError("the entry has neither equals nor approx")
            got, want = lookup(doc, p), e[kind]
            num = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (got, want))
            same = abs(got - want) <= 1e-3 * abs(want) if kind == "approx" and num else kind == "equals" and got == want
            if not same:
                raise LookupError(f"expected {kind} {want!r}, got {got!r}")
            out(f"ok    {p} {kind} {want!r}{src}")
        except LookupError as err:
            bad += 1
            out(f"DIFF  {p}: {err.args[0]}{src}")
    return 1 if bad or not entries else 0


# ---------------------------------------------------------------------------------------------- self-test

def _toy(d, **over):
    """A small finished run shaped like a stored v2 run: signed spec, two artifacts with headers, a manifest."""
    def w(rel, text):
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text if isinstance(text, str) else json.dumps(text))
        return hashlib.sha256(open(p, "rb").read()).hexdigest()
    spec = w("decision_spec.yaml", "question: toy\n")
    units = w("data/units.csv", "unit,pop\na,1\n")
    w("decision_spec.signoff.json", {"signed_by": "owner", "signed_at": "2026-01-01T00:00:00+00:00",
                                     "sha256": {"decision_spec.yaml": spec}})
    head = "schema: analysis.{k}/1\nproducer: toolkit/analysis/{k}.py\nspec_sha256: {s}\nspec_signed: true\n" \
           "engine: {{name: toy, version: '0.1'}}\nwritten_at: '2026-01-01T01:00:00+00:00'\ninputs:\n{i}data:\n  x: 1\n"
    point = w("analysis/point.yaml", head.format(k="point", s=spec, i=f"  decision_spec.yaml: {spec}\n  data/units.csv: {units}\n"
                                                 "  ../../toolkit/engines/toy/card.yaml: abc\n"))
    claims = w("analysis/claims.yaml", head.format(k="claims", s=spec, i=f"  analysis/point.yaml: {point}\n") + "  big: " + "y" * 10)
    alloc = w("allocation.csv", "unit,package\na,p\n")
    w("analysis/manifest.json", {"complete": True, "artifacts": {
        "analysis/point.yaml": {"kind": "point", "producer": "toolkit/analysis/point.py", "sha256": point},
        "analysis/claims.yaml": {"kind": "claims", "producer": "toolkit/analysis/claims.py", "sha256": claims}},
        "deliverables": {"allocation.csv": {"producer": "toolkit/analysis/run.py", "role": "allocation", "sha256": alloc}}})
    w("metadata.json", {"pipeline": "v2", "commit": "", "started": "2026-01-01T00:00:00+00:00", "status": "completed",
                        "invocations": [{"pipeline": "v2", "commit": "", "at": "2026-01-01T00:00:00"}], **over.get("meta", {})})
    hist = over.get("history") or [("SPEC", "SIGN", 1.0, 5), ("SIGN", "ANALYZE", 1.0, 6), ("ANALYZE", "CRITIQUE", 1.0, 66),
                                   ("CRITIQUE", "GATE", 2.5, 70), ("GATE", "WRITE", 2.5, 71), ("WRITE", "FINAL", 3.0, 75)]
    w("run_state.json", {"question": "toy question", "state": "FINAL", "status": "completed", "paused": None, "round": 1,
                         "budget_usd": 30.0, "spent_usd": hist[-1][2],
                         "history": [{"at": f"2026-01-01T{m // 60:02d}:{m % 60:02d}:00+00:00", "from": a, "to": b,
                                      "why": "ok", "round": 1, "spent_usd": u} for a, b, u, m in hist],
                         "journal": {"SPEC#0:try1": {"task": "draft_spec", "status": "completed", "ok": True, "usd": 1.0,
                                                     "allowance_usd": 3.0, "started": "2026-01-01T00:00:00+00:00",
                                                     "completed": "2026-01-01T00:05:00+00:00", "note": "drafted"},
                                     **over.get("journal", {})}, **over.get("state", {})})
    return w


def _self_test():
    fails = []

    def ok(name, cond, detail=""):
        print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f": {detail}" if not cond and detail else ""))
        if not cond:
            fails.append(name)

    def st(run, cid):
        return next(c for c in checks.run_all(run) if c["id"] == cid)

    with tempfile.TemporaryDirectory() as tmp:
        d = os.path.join(tmp, "keep", "job", "out", "runs", "2026-01-01_000000_toy-question-about-a-thing")
        os.makedirs(d)
        w = _toy(d)
        wb = Workbench([os.path.join(tmp, "keep")])
        runs = wb.api_runs()["runs"]
        ok("W1 discovery finds the run and labels it by its place under the root",
           len(runs) == 1 and runs[0]["label"] == "job / 2026-01-01" and runs[0]["pipeline"] == "v2", str(runs))
        rid = runs[0]["id"]
        v, over = wb.api_view(rid, "steps"), wb.api_view(rid, "overview")

        def table(t):
            return [dict(zip(t["columns"], r)) for r in t["rows"]]
        by = {c["id"]: c["status"] for c in v["checks"]}
        ok("W2 a clean finished run: every check ok except the commit (none recorded: problem)",
           {k: s for k, s in by.items() if s != "ok"} == {"steps.pipeline": "problem"}, str(by))
        ok("W3 every check names the file and field it read, and one plain sentence",
           all(c["says"] and c["evidence"] for c in v["checks"]))
        a = {x["file"]: x for x in table(v["tables"]["artifacts"])}
        ok("W4 artifact headers read from the file's first bytes; a harness input is marked outside the run folder",
           a["analysis/point.yaml"]["producer"] == "toolkit/analysis/point.py"
           and [i["state"] for i in table(v["tables"]["inputs"]) if i["file"] == "analysis/point.yaml"]
           == ["outside", "same", "same"], str(v["tables"]["inputs"]))
        hist = table(v["tables"]["history"])
        ok("W5 steps: time and spend per transition", [h["model_spend_usd"] for h in hist] == [1.0, 0.0, 0.0, 1.5, 0.0, 0.5]
           and hist[2]["minutes"] == 60.0, str(hist))
        path = {p["view"]: p for p in table(over["tables"]["views"])}
        ok("W6 the overview lists views not built yet with the goal that builds them, and each view's worst status",
           (path["spec"]["status"], path["spec"]["goal"]) == ("not_built", "G6") and path["steps"]["status"] == "problem"
           and "Spec and evidence (goal G6)" in over["notes"]["not_built"], str(path))

        w("decision_spec.yaml", "question: toy, redrafted after signing\n")
        r = Run(d)
        ok("W7 C10: a spec edited after signing is a problem, and the artifact that read it is stale",
           st(r, "steps.signed_spec")["status"] == "problem" and st(r, "steps.inputs_fresh")["status"] == "problem"
           and st(r, "steps.inputs_fresh")["rows"]["rows"][0][:2] == ["analysis/point.yaml", "decision_spec.yaml"])
        ok("W8 C10: artifacts still record the signed spec's hash, so that check stays ok",
           st(r, "steps.artifact_spec")["status"] == "ok")
        w("allocation.csv", "unit,package\na,edited\n")
        ok("W9 a deliverable changed after the analysis wrote it is a problem", st(Run(d), "steps.manifest")["status"] == "problem")

        d2 = os.path.join(tmp, "keep", "loop")
        os.makedirs(d2)
        _toy(d2, history=[("SPEC", "SIGN", 1, 1), ("CRITIQUE", "GATE", 1, 2), ("GATE", "CRITIQUE", 1, 3),
                          ("CRITIQUE", "GATE", 1, 4), ("GATE", "CRITIQUE", 1, 5), ("CRITIQUE", "FINAL", 1, 6)],
             state={"status": "run_failed"},
             journal={"SPEC#1:try2": {"task": "draft_spec", "status": "completed", "ok": False, "usd": 2.9,
                                      "allowance_usd": 3.0, "note": "ResultError"}})
        r2 = Run(d2)
        ok("W10 C16: repeated transitions and a failed task are a problem naming the task",
           st(r2, "steps.progress")["status"] == "problem" and "SPEC#1:try2" in st(r2, "steps.progress")["says"])
        ok("W11 a run that ended run_failed is a problem; a task at its allowance is flagged",
           st(r2, "steps.outcome")["status"] == "problem" and st(r2, "steps.cost")["status"] == "look")

        d3 = os.path.join(tmp, "keep", "broken")
        os.makedirs(d3)
        open(os.path.join(d3, "run_state.json"), "w").write("{not json")
        ok("W12 fail closed: an unreadable run gives not_available everywhere, never a crash or an ok",
           all(c["status"] == "not_available" for c in checks.run_all(Run(d3))), str([c["status"] for c in checks.run_all(Run(d3))]))
        os.symlink("/etc/hosts", os.path.join(d3, "metadata.json"))
        for rel in ("../x", ".env", "data/id_rsa.pub", "data/.env.local"):       # harmless files, so "not found" cannot pass for a refusal
            open(os.path.join(d, rel), "w").write("harmless\n")
        refused = {rel: Run(d).read(rel) for rel in ("../x", ".env", "data/id_rsa.pub", "data/.env.local", "data/units.csv")}
        ok("W13 confined reads: no link followed, no path outside the run, no credential file, each refused for its own reason "
           "although the file exists",
           Run(d3).read("metadata.json")[0] is None and "link" in Run(d3).read("metadata.json")[1]
           and refused["../x"] == (None, "../x could not be read ('../x': not a relative path inside the run directory)")
           and all(refused[rel] == (None, f"{rel} could not be read ({rel}: credential files are not read)")
                   for rel in (".env", "data/id_rsa.pub", "data/.env.local")) and refused["data/units.csv"][0] is not None, str(refused))
        ok("W14 the server answers loopback GETs only",
           wb.handle("GET", "/api/runs", "evil.example")[0] == 403 and wb.handle("POST", "/api/runs", "127.0.0.1:1")[0] == 405
           and wb.handle("GET", "/api/runs", "127.0.0.1:8790")[0] == 200 and wb.handle("GET", "/api/view?run=nope&view=steps", "localhost")[0] == 404)
        ok("W15 the page and its script are served; the script builds no HTML from run text",
           wb.handle("GET", "/", "localhost")[0] == 200
           and not re.search(r"innerHTML|insertAdjacentHTML|document\.write", open(os.path.join(STATIC, "app.js")).read()))
        page = open(os.path.join(STATIC, "index.html")).read()
        ok("W16 the page holds no inline script or style, and every static file is served under the policy's 'self'",
           not re.search(r"<style|style=|<script(?![^>]*\bsrc=)[^>]*>|\bon\w+=|https?://", page)
           and all(wb.handle("GET", u, "localhost")[0] == 200 for u in STATIC_FILES))

        code, ctype, body = wb.handle("GET", f"/api/view?run={rid}&view=steps", "localhost")
        doc = json.loads(body)
        ok("W17 the data endpoint returns checks, facts, tables, charts and notes, and the Steps tables",
           code == 200 and sorted(doc) == sorted(KEYS) and len(doc["checks"]) == 8
           and {"history", "tasks", "artifacts", "inputs"} <= set(doc["tables"])
           and all(len(r) == len(t["columns"]) for t in doc["tables"].values() for r in t["rows"]), str(sorted(doc)))
        cols = {n: t["columns"] for n, t in doc["tables"].items()}
        ok("W18 every chart names a kind the page draws, a table of the result and columns of that table",
           [c["kind"] for c in doc["charts"]] == ["graph", "bars", "bars"]
           and all(c["title"] and c["how_to_read"] and all(c[k] in cols[c["table"]] for k in ("x", "y", "group") if k in c)
                   for c in doc["charts"]) and cols[doc["charts"][0]["nodes"]][0] == "file", str(doc["charts"]))
        ok("W19 an unknown view is a 404; a view that is not built says so with its goal, and is not a 500",
           wb.handle("GET", f"/api/view?run={rid}&view=nope", "localhost")[0] == 404
           and wb.api_view(rid, "spec")["checks"][0]["status"] == "not_available"
           and "goal G6" in wb.api_view(rid, "spec")["checks"][0]["says"])
        global _view_module
        real, in_folder = _view_module, [n for n, _, _ in PATH if os.path.exists(os.path.join(HERE, "views", f"{n}.py"))]

        def broken(name):
            raise ImportError("planted")
        try:
            listed = [n for n, _, _ in PATH if real(n) is not None]
            _view_module = broken
            why, c = load_view("spec")[1], wb.api_view(rid, "spec")["checks"][0]
        finally:
            _view_module = real
        ok("W19b the view registry is a list of import statements that names every view module in views/ and no other; "
           "a view module that cannot be imported gives not_available with the reason, never a crash",
           listed == in_folder and "could not be loaded" in why and c["status"] == "not_available" and "planted" in c["says"],
           str((listed, in_folder, why, c)))

        def boom(run):
            raise KeyError("no such field")
        VIEW_FNS.update(spec=boom, model=lambda run: ["not", "a", "result"],
                        fit=lambda run: {"facts": {"x": float("nan")}, "tables": {}})
        try:
            code, _, body = wb.handle("GET", f"/api/view?run={rid}&view=spec", "localhost")
            c = json.loads(body)["checks"][0]
            ok("W20 a view that raises gives not_available with the reason, never a 500",
               code == 200 and c["status"] == "not_available" and "KeyError" in c["says"], str(c))
            ok("W21 a view returning another shape is not_available; missing parts are filled; a NaN reaches the page as null",
               wb.api_view(rid, "model")["checks"][0]["status"] == "not_available"
               and wb.api_view(rid, "fit") == {"checks": [], "facts": {"x": None}, "tables": {}, "charts": [], "notes": {}})
            o = wb.api_view(rid, "overview")
            mine = [c for c in o["checks"] if c["step"] in ("spec", "model", "fit", "steps")]   # the views this case sets up
            order = [c["status"] for c in mine]
            ok("W22 the overview gathers the checks of every view that exists: views in reading order whatever their "
               "statuses, worst first within a view, each check naming its view",
               [c["step"] for c in mine] == ["spec", "model"] + ["steps"] * 8 and order[2] == "problem"
               and order[2:] == sorted(order[2:], key=checks.WORST_FIRST.index)
               # the views between them in reading order (Outcomes, once it exists) keep their own place
               and [c["step"] for c in o["checks"] if c["step"] in ("spec", "model")] == ["spec", "model"]
               and [c["step"] for c in o["checks"]].index("steps") > [c["step"] for c in o["checks"]].index("model")
               and o["facts"]["checks_problem"] == [c["status"] for c in o["checks"]].count("problem") and o["facts"]["question"] == "toy question", str(order))
        finally:
            for k in ("spec", "model", "fit"):
                VIEW_FNS.pop(k)

        acc, lines = os.path.join(tmp, "steps.yaml"), []
        entries = [{"path": "facts.status", "equals": "completed", "source": "run_state.json status"},
                   {"path": "facts.model_spend_usd", "approx": 3.0 + 1e-3, "source": "run_state.json spent_usd"},
                   {"path": "tables.tasks[key=SPEC#0:try1].allowance_usd", "equals": 3.0},
                   {"path": "tables.artifacts[file=analysis/point.yaml].kind", "equals": "point"},
                   {"path": "checks[id=steps.pipeline].status", "equals": "problem"}]
        open(acc, "w").write(yaml.safe_dump(entries))
        good = accept(wb, d, acc, lines.append)
        open(acc, "w").write(yaml.safe_dump(entries + [{"path": "facts.model_spend_usd", "approx": 3.1},
                                                       {"path": "tables.tasks[key=none].ok", "equals": True},
                                                       {"path": "facts.status"}]))
        bad = []
        ok("W23 --accept: one line per entry; 0 when every quoted value matches, 1 on any difference",
           good == 0 and len(lines) == 5 and all(x.startswith("ok ") for x in lines) and accept(wb, d, acc, bad.append) == 1
           and [x[:4] for x in bad] == ["ok  "] * 5 + ["DIFF"] * 3 and accept(wb, d, os.path.join(tmp, "none.yaml"), bad.append) == 1,
           str(lines + bad))

        d4 = os.path.join(tmp, "keep", "declared")
        os.makedirs(d4)
        _toy(d4, state={"status": "completed_scope_declared"})
        c = st(Run(d4), "steps.outcome")
        ok("W24 a run that ended with a declared scope is for the reader to judge (look), not a problem",
           c["status"] == "look" and "declared" in c["says"] and st(r2, "steps.outcome")["status"] == "problem")
        _toy(d4, history=[("SPEC", "SIGN", 1.0, 5), ("SIGN", "ANALYZE", 1.0, 65), ("ANALYZE", "FINAL", 2.0, 75)])
        s4 = json.load(open(os.path.join(d4, "run_state.json")))
        s4["history"].insert(1, {"at": "2026-01-01T00:30:00+00:00", "fix_loop": {"round": 1}})
        open(os.path.join(d4, "run_state.json"), "w").write(json.dumps(s4))
        v4 = Run(d4).view("steps")
        ok("W25 a history entry that is not a transition is left out; time waiting for the owner is counted apart",
           len(v4["tables"]["history"]["rows"]) == 3 and v4["facts"]["waiting_for_owner_minutes"] == 60.0
           and v4["facts"]["working_minutes"] == 15.0, str(v4["facts"]))

        d5 = os.path.join(tmp, "keep", "large")
        os.makedirs(os.path.join(d5, "analysis"))
        big = os.path.join(d5, "analysis", "big.yaml")
        open(big, "w").write("schema: toy/1\ndata:\n  rows:\n" + "".join(f"  - [{i}, {i * 0.5}]\n" for i in range(2000)))
        n0 = LARGE_PARSES[0]
        first, again = Run(d5).large("analysis/big.yaml"), Run(d5).large("analysis/big.yaml")
        ok("W26 a large YAML file is parsed once and kept in memory: a second Run of the folder gets the same document",
           first[1] is None and first[0]["data"]["rows"][1999] == [1999, 1999 * 0.5] and again[0] is first[0]
           and LARGE_PARSES[0] == n0 + 1, str((first[1], LARGE_PARSES[0] - n0)))
        open(big, "a").write("  - [2000, 1000.0]\n")
        later = Run(d5).large("analysis/big.yaml")
        ok("W27 the kept document is keyed by size and modification time: a changed file is parsed again",
           later[0] is not first[0] and len(later[0]["data"]["rows"]) == 2001 and LARGE_PARSES[0] == n0 + 2)
        os.symlink(big, os.path.join(d5, "analysis", "link.yaml"))
        open(os.path.join(d5, "analysis", "bad.yaml"), "w").write("a: [unclosed\n  - {")
        ok("W28 the large reader fails closed: no link followed, no path outside the run, a reason for a file it cannot parse",
           Run(d5).large("analysis/link.yaml")[0] is None and Run(d5).large("../large/analysis/big.yaml")[0] is None
           and Run(d5).large("analysis/none.yaml") == (None, "analysis/none.yaml is not in the run folder")
           and Run(d5).large("analysis/bad.yaml")[0] is None and "parsed" in Run(d5).large("analysis/bad.yaml")[1])

        script = open(os.path.join(STATIC, "app.js")).read()
        good = {"tables": {"t": {"columns": ["a", "b", "c"], "rows": [["x", 1, 2]]}}, "charts": [
            {"kind": "bars", "table": "t", "x": "a", "y": "b", "title": "T", "how_to_read": "H", "where": {"a": "x"},
             "panels": "a", "labels": {"b": "b (count)"}, "limits": [{"value": 1, "label": "limit"}]}]}
        wrong = [dict(good["charts"][0], **edit) for edit in (
            {"kind": "pie"}, {"colour": "red"}, {"y": "z"}, {"where": {"z": 1}}, {"labels": {"z": "?"}},
            {"panels": "z"}, {"limits": [3]}, {"title": ""}, {"table": "none"})]
        ok("W29 chart declarations: every documented key is read by the page; an unknown kind, key, table or column, "
           "a missing title, and limits that are not {value, label} are each reported",
           chart_problems(good) == [] and all(len(chart_problems(dict(good, charts=[w]))) == 1 for w in wrong)
           and all(re.search(rf"\b(decl|c|d)\.{k}\b", script) for k in CHART_KEYS)
           and all(k in __doc__ for k in CHART_KEYS),
           str([chart_problems(dict(good, charts=[w])) for w in wrong]
               + [k for k in CHART_KEYS if not re.search(rf"\b(decl|c|d)\.{k}\b", script)]))
        for name in ("steps", "fit"):
            vv = wb.api_view(rid, name)
            ok(f"W30 the {name} view's own chart declarations on the toy run are ones the page can draw",
               chart_problems(vv) == [], str(chart_problems(vv)))

        code, _, body = wb.handle("GET", "/api/glossary", "localhost")
        g = json.loads(body) if code == 200 else {}
        codes = {(e["kind"], e["code"]) for e in g.get("entries") or []}
        from views import fit as fit_view
        used = ({("scorecard case", c["case"]) for c in over["checks"] if c["case"]}
                | {("scorecard case", c) for c in fit_view.CHECKS.values() if c} | {("goal", goal) for _, _, goal in PATH})
        ok("W31 the glossary is served from the specification's own tables: it defines every scorecard case a check "
           "names and every goal of the rail, and the page script holds no meaning of its own",
           g.get("why") is None and used <= codes and all(e["meaning"] for e in g["entries"])
           and "/api/glossary" in script and not any(e["meaning"][:40] in script for e in g["entries"]),
           str((code, g.get("why"), sorted(used - codes))))
        lost = Workbench([], spec=os.path.join(tmp, "no-spec.md"))
        gl = json.loads(lost.handle("GET", "/api/glossary", "localhost")[2])
        ok("W32 fail closed: with no readable specification the glossary is empty and says why",
           gl["entries"] == [] and "could not be read" in str(gl["why"]), str(gl))

        d6, t0 = os.path.join(tmp, "keep", "running"), 1_700_000_000
        os.makedirs(d6)
        _toy(d6, state={"state": "ANALYZE", "status": None})
        for root, _, names in os.walk(d6):
            for n in names:
                os.utime(os.path.join(root, n), (t0, t0))
        os.utime(os.path.join(d6, "analysis", "point.yaml"), (t0 + 600, t0 + 600))
        os.symlink("/etc/hosts", os.path.join(d6, "analysis", "link.yaml"))
        lw, later = Run(d6).last_written(), Run(d6, now=t0 + 600 + 2 * 3600 + 60)
        ok("W33 the last file written is the newest among the run's files (no link followed); the walk is bounded and says "
           "when it was cut short", lw == {"at": t0 + 600, "file": "analysis/point.yaml", "cut": False}
           and Run(d6).last_written(limit=2)["cut"] is True and Run(os.path.join(tmp, "none")).last_written()["at"] is None, str(lw))
        summ = later.summary()
        ok("W34 time since the last file was written, in plain words, in the run summary and the Steps outcome sentence",
           [ago(x) for x in (30, 90, 59 * 60, 2 * 3600 + 60, 26 * 3600, 3 * 86400 + 5)]
           == ["less than a minute", "1 minute", "59 minutes", "2 hours", "1 day", "3 days"]
           and (summ["last_written"], summ["last_written_file"], summ["last_written_ago"]) == (t0 + 600, "analysis/point.yaml", "2 hours")
           and "2 hours ago" in st(later, "steps.outcome")["says"] and "analysis/point.yaml" in st(later, "steps.outcome")["says"]
           and "2 hours ago" in str(later.view("overview")["facts"].get("last_file_written")),
           str((summ, st(later, "steps.outcome")["says"])))

        alone = Workbench(chosen_roots(None, [d]), [d])
        listed = alone.api_runs()
        ok("W35 a run given by --run-dir is listed as that folder: the default root is not scanned or named beside it",
           chosen_roots(None, [d]) == [] and chosen_roots(None, []) == [os.path.join(REPO, "runs")]
           and chosen_roots(["x"], [d]) == ["x"] and listed["roots"] == [] and listed["run_dirs"] == [os.path.realpath(d)]
           and len(listed["runs"]) == 1, str({k: listed[k] for k in ("roots", "run_dirs")}))

        # ---- cases from the code review of the workbench (R<n>: the finding's number) ----
        def toy(name):
            dd = os.path.join(tmp, "review", name)
            os.makedirs(dd)
            _toy(dd)
            return dd

        def edit_json(dd, rel, change):
            doc = json.load(open(os.path.join(dd, rel)))
            change(doc)
            open(os.path.join(dd, rel), "w").write(json.dumps(doc))

        def states(c):
            return {r[0] if c["id"] != "steps.inputs_fresh" else r[1]: r[-1] for r in (c["rows"] or {}).get("rows", [])}
        d7 = toy("lost-manifest")
        edit_json(d7, "analysis/manifest.json", lambda m: m["artifacts"].update({"analysis/lost.yaml": {}}))
        c = st(Run(d7), "steps.manifest")
        ok("R10a a listed file that is missing never matches a manifest entry that records no hash",
           c["status"] == "problem" and states(c) == {"analysis/lost.yaml": "missing"}, str(c))
        edit_json(d7, "analysis/manifest.json", lambda m: (m["artifacts"].pop("analysis/lost.yaml"),
                                                           m["deliverables"].update({"allocation.csv": {"sha256": "abc"}})))
        c = st(Run(d7), "steps.manifest")
        ok("R10b a file the manifest records no valid hash for is not said to have changed: no hash was recorded",
           c["status"] == "problem" and states(c) == {"allocation.csv": "no valid hash recorded"}, str(c))
        d7 = toy("lost-signed")
        edit_json(d7, "decision_spec.signoff.json", lambda so: so["sha256"].update({"data/gone.csv": None, "data/lost.csv": "0" * 64}))
        c = st(Run(d7), "steps.signed_spec")
        ok("R10c a signed file that is missing is never the same as what was signed, whatever hash was recorded",
           c["status"] == "problem" and states(c) == {"data/gone.csv": "missing", "data/lost.csv": "missing", "decision_spec.yaml": "same"}, str(c))
        # toolkit/spec/signoff.py sign() records null for the derivation record when the run has none, and
        # derived_changed() accepts it while the file stays absent (`if signed is None and now is None: return []`)
        d7, derived = toy("absent-signed"), "data/derivations_record.json"
        edit_json(d7, "decision_spec.signoff.json", lambda so: so["sha256"].update({derived: None}))
        c = st(Run(d7), "steps.signed_spec")
        ok("R10h a file the producer records null for (absent when signed) and that is still absent is unchanged, and said so",
           c["status"] == "ok" and states(c) == {derived: "absent when signed, absent now", "decision_spec.yaml": "same"}
           and "absent when signed" in c["says"], str(c))
        os.makedirs(os.path.join(d7, "data"), exist_ok=True)
        open(os.path.join(d7, derived), "w").write("{}")
        c = st(Run(d7), "steps.signed_spec")
        ok("R10i the same file present now appeared after signing: a problem",
           c["status"] == "problem" and states(c) == {derived: "appeared after signing", "decision_spec.yaml": "same"}
           and "appeared after signing" in c["says"], str(c))
        os.remove(os.path.join(d7, derived))
        os.symlink("units.csv", os.path.join(d7, derived))
        c = st(Run(d7), "steps.signed_spec")
        ok("R10j a link at that name is not an absent file: never a match",
           c["status"] == "problem" and states(c)[derived] == "missing", str(c))
        d7 = toy("lost-signed-2")
        edit_json(d7, "decision_spec.signoff.json", lambda so: so["sha256"].update({"data/gone.csv": None}))
        edit_json(d7, "decision_spec.signoff.json", lambda so: (so["sha256"].pop("data/gone.csv"), so["sha256"].update({"data/units.csv": "abc"})))
        c = st(Run(d7), "steps.signed_spec")
        ok("R10d a file signed with no valid hash is not said to be unchanged",
           c["status"] == "problem" and states(c) == {"data/units.csv": "no valid hash recorded", "decision_spec.yaml": "same"}, str(c))
        d7 = toy("lost-input")
        claims = open(os.path.join(d7, "analysis", "claims.yaml")).read()
        open(os.path.join(d7, "analysis", "claims.yaml"), "w").write(claims.replace("inputs:\n", "inputs:\n  data/absent.csv: null\n", 1))
        c = st(Run(d7), "steps.inputs_fresh")
        ok("R10e a recorded input that is missing is never fresh, whatever hash was recorded",
           c["status"] == "problem" and states(c) == {"data/absent.csv": "missing"}, str(c))
        open(os.path.join(d7, "analysis", "claims.yaml"), "w").write(claims.replace("inputs:\n", "inputs:\n  allocation.csv: ''\n", 1))
        c = st(Run(d7), "steps.inputs_fresh")
        ok("R10f an input recorded with no valid hash is never fresh",
           c["status"] == "problem" and states(c) == {"allocation.csv": "not_recorded"}, str(c))
        d7 = toy("nohash-spec")
        edit_json(d7, "decision_spec.signoff.json", lambda so: so["sha256"].update({"decision_spec.yaml": "abc"}))
        for name in ("point", "claims"):
            text = open(os.path.join(d7, "analysis", f"{name}.yaml")).read()
            open(os.path.join(d7, "analysis", f"{name}.yaml"), "w").write(re.sub(r"spec_sha256: \w+", "spec_sha256: abc", text))
        c = st(Run(d7), "steps.artifact_spec")
        ok("R10g a signed spec hash that is no digest is nothing an analysis file can be said to match",
           c["status"] == "not_available", str(c))

        outside = os.path.join(tmp, "elsewhere")
        os.makedirs(os.path.join(outside, "deep"))
        open(os.path.join(outside, "private_name.yaml"), "w").write("schema: toy/1\ndata: {}\n")
        open(os.path.join(outside, "deep", "newest.txt"), "w").write("x")
        d8 = toy("linked-analysis")
        shutil.rmtree(os.path.join(d8, "analysis"))
        os.symlink(outside, os.path.join(d8, "analysis"))
        listed = [a["path"] for a in Run(d8).artifacts()] + [r[0] for r in Run(d8).view("steps")["tables"].get("artifacts", {}).get("rows", [])]
        ok("R2a a linked analysis folder is not listed: no file name from outside the run reaches the artifacts table",
           not any("private_name" in x for x in listed), str(listed))
        d8 = toy("swapped-folder")
        os.makedirs(os.path.join(d8, "sub"))
        open(os.path.join(d8, "sub", "inside.txt"), "w").write("x")
        future = time.time() + 5000
        os.utime(os.path.join(outside, "deep", "newest.txt"), (future, future))
        real_scandir, swapped = os.scandir, []

        class Listed(list):          # what os.scandir gives, already read: usable in a `with`, as os.scandir's is
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def swapping(where):         # the folder queued for the walk is replaced by a link before it is opened
            if not swapped:
                swapped.append(1)
                entries = Listed(real_scandir(where))
                os.rename(os.path.join(d8, "sub"), os.path.join(d8, "sub_was"))
                os.symlink(os.path.join(outside, "deep"), os.path.join(d8, "sub"))
                return entries
            return real_scandir(where)
        os.scandir = swapping
        try:
            lw = Run(d8).last_written()
        finally:
            os.scandir = real_scandir
        ok("R2b the walk for the last file written opens each folder with no link followed: a folder replaced by a link "
           "after it was listed is not entered", swapped and "newest" not in str(lw["file"]) and (lw["at"] or 0) < future - 1, str(lw))

        d9 = toy("big-hash")
        with open(os.path.join(d9, "data", "big.bin"), "wb") as f:
            f.truncate(48 << 20)
        import tracemalloc
        tracemalloc.start()
        got = Run(d9).sha("data/big.bin")
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        zeros = hashlib.sha256()
        for _ in range(48):
            zeros.update(bytes(1 << 20))
        ok("R4 a file is hashed piece by piece: hashing a 48 MiB file holds a few MiB at most",
           got == zeros.hexdigest() and peak < (8 << 20), f"peak {peak >> 20} MiB")

        d10, other = toy("hard-link"), toy("other-run")
        edit_json(other, "metadata.json", lambda m: m.update(question="the other run's question"))
        os.remove(os.path.join(d10, "metadata.json"))
        os.link(os.path.join(other, "metadata.json"), os.path.join(d10, "metadata.json"))
        os.link(os.path.join(other, "analysis", "point.yaml"), os.path.join(d10, "analysis", "linked.yaml"))
        r10 = Run(d10)
        whys = [r10.read("metadata.json"), r10.json("metadata.json"), r10.large("analysis/linked.yaml")]
        ok("R3 a file with more than one hard link is refused, by every reader, and the reason says so",
           all(v is None and "hard link" in str(why) for v, why in whys) and r10.sha("analysis/linked.yaml") is None
           and "other run" not in json.dumps(r10.summary()), str(whys))

        wb2 = Workbench([os.path.join(tmp, "review2")])
        d11 = os.path.join(tmp, "review2", "mine")
        os.makedirs(d11)
        _toy(d11)
        rid2 = next(iter(wb2.scan()))
        os.rename(d11, os.path.join(tmp, "mine-moved"))
        os.symlink(other, d11)
        v2 = wb2.api_view(rid2, "steps")
        ok("R1 a run folder replaced after discovery is not read: every check is not_available and says the folder changed",
           v2 is None or (all(c["status"] == "not_available" for c in v2["checks"]) and "other run" not in json.dumps(v2)
                          and any("device and inode" in c["says"] for c in v2["checks"])),
           str([(c["status"], c["says"][:90]) for c in (v2 or {}).get("checks", [])]))
        ok("R1b the run list shows nothing read from the replacement either",
           "other run" not in json.dumps(wb2.api_runs()), json.dumps(wb2.api_runs())[:300])

        _self_test_notes(tmp, ok)
        _self_test_files(tmp, ok)
        _self_test_against(tmp, ok)

        node = shutil.which("node")
        if node is None:
            print("  skip W36 the page script's own cases (page_test.js): node is not installed here")
        else:
            r = subprocess.run([node, os.path.join(HERE, "page_test.js")], capture_output=True, text=True, timeout=120)
            ok("W36 the page script's own cases pass (page_test.js: ticks, collapsed checks, view order, glossary, run list, images)",
               r.returncode == 0, (r.stdout + r.stderr)[-900:])
    print("OK: all self-test cases passed." if not fails else f"FAILED: {fails}")
    return 1 if fails else 0


def _read_file(tmp, built, d, rel):
    """A fixture file as the builder wrote it (from a second build), to put back what a test changed."""
    from views.fit_bench import fixture_builder
    again = os.path.join(tmp, "imaged", "again")
    if not os.path.isdir(again):
        fixture_builder().build(again)
    with open(os.path.join(again, rel), "rb") as f:
        return f.read()


def _self_test_files(tmp, ok):
    """§5.12, the file endpoint, written before it is built: GET /api/file?run=<id>&path=<rel>&sha=<hash> serves an
    image of a run to the page. Each case holds it to one rule: only a file inside the run, read with no link followed;
    only with the hash the request names; only when a view of that run has declared that path with that hash (its
    `images`: {table, path, hash, title, caption} over one of its tables); only an image; under a sandboxed content
    policy; loopback Host only. What the cases assume where §5.12 is silent: `images` is a sixth key of a view's result,
    which /api/view passes through; Workbench.handle answers the endpoint with a fourth element, the headers to send
    beside the usual ones ({name: value}), since the content policy of an image is not the page's; a refusal is any
    status but 200, with none of the file's bytes in the body."""
    from views.fit_bench import fixture_builder
    d = os.path.join(tmp, "imaged", "run")
    built = fixture_builder().build(d)
    wb = Workbench([], [d])
    rid = next(iter(wb.scan()))
    image = "figures/headline.png"
    with open(os.path.join(d, image), "rb") as f:
        png = f.read()

    def put(rel, data=png):
        with open(os.path.join(d, rel), "wb") as f:
            f.write(data)
        return hashlib.sha256(data).hexdigest()
    outside = os.path.join(tmp, "imaged", "elsewhere.png")
    with open(outside, "wb") as f:
        f.write(png + b"elsewhere")
    os.symlink("headline.png", os.path.join(d, "figures", "linked.png"))
    os.link(outside, os.path.join(d, "figures", "also_elsewhere.png"))
    secret = {rel: put(rel, png + rel.encode()) for rel in ("figures/id_rsa.png", "figures/credentials.png", ".env")}
    other = {rel: built[rel] for rel in ("figures/headline.csv", "figures/headline.svg", "analysis/point.yaml", "report.md")}
    changed = put("figures/frontier.png", png + b"drawn again")           # replaced after the view named its first hash
    named = {image: built[image], "figures/parameters.png": "0" * 64, "figures/frontier.png": built["figures/frontier.png"],
             "figures/linked.png": built[image], "figures/also_elsewhere.png": hashlib.sha256(png + b"elsewhere").hexdigest(),
             "../elsewhere.png": hashlib.sha256(png + b"elsewhere").hexdigest(), outside: hashlib.sha256(png + b"elsewhere").hexdigest(),
             **secret, **other}
    declared = [{"table": "images", "path": "path", "hash": "sha256", "title": "title", "caption": "caption"}]
    VIEW_FNS["spec"] = lambda run: {"checks": [], "facts": {}, "charts": [], "notes": {}, "images": declared, "tables": {"images": {
        "columns": ["figure", "path", "sha256", "title", "caption"], "about": "planted",
        "rows": [[os.path.basename(rel), rel, sha, None, None] for rel, sha in named.items()]}}}

    def get(rel, sha, host="127.0.0.1:8790", run=rid, method="GET"):
        q = urllib.parse.urlencode({"run": run, "path": rel, "sha": sha})
        out = wb.handle(method, f"/api/file?{q}", host)
        return out[0], out[1], out[2], (out[3] if len(out) > 3 else {})

    def refused(rel, sha, data=png, **how):
        code, _, body, _ = get(rel, sha, **how)
        return code != 200 and (data[8:] not in body)
    try:
        ok("W37 the rail lists Figures between Steps and the Science register, built by goal G2b (§9, §10)",
           [(i, g) for i, _, g in PATH][6:9] == [("steps", "G1"), ("figures", "G2b"), ("register", "G3")], str([i for i, _, _ in PATH]))
        ok("W38 /api/view passes a view's `images` declarations through beside checks, facts, tables, charts and notes",
           (wb.api_view(rid, "spec") or {}).get("images") == declared, str(sorted(wb.api_view(rid, "spec") or {})))
        code, ctype, body, headers = get(image, built[image])
        ok("W39 the file endpoint serves an image a view named, by the hash the view named, as image/png",
           (code, ctype, body) == (200, "image/png", png), str((code, ctype, len(body))))
        policy = {k.lower(): v for k, v in headers.items()}.get("content-security-policy", "")
        ok("W40 an image is sent under a sandboxed content policy that allows nothing (sandbox; default-src 'none'), not the page's",
           code == 200 and "sandbox" in policy and "default-src 'none'" in policy and "script-src" not in policy, str(headers))
        ok("W41 it refuses a file of the run that no view named, although the request gives its true hash",
           get(image, built[image])[0] == 200 and refused("figures/timeline.png", built["figures/timeline.png"])
           and refused("allocation.csv", built["allocation.csv"]))
        ok("W42 it refuses a hash other than the one the view named for that path, and a path named with a hash the file does not have",
           get(image, built[image])[0] == 200 and refused(image, "0" * 64) and refused(image, "") and refused(image, built["figures/frontier.png"])
           and refused("figures/parameters.png", "0" * 64) and refused("figures/parameters.png", built["figures/parameters.png"]))
        ok("W43 it refuses a file that changed after the view named it: the bytes served always have the hash in the request",
           get(image, built[image])[0] == 200 and refused("figures/frontier.png", built["figures/frontier.png"], png + b"drawn again")
           and refused("figures/frontier.png", changed, png + b"drawn again"))
        ok("W44 it refuses a path outside the run, relative or absolute, although a view named it with the file's hash",
           get(image, built[image])[0] == 200 and refused("../elsewhere.png", named["../elsewhere.png"], png + b"elsewhere")
           and refused(outside, named[outside], png + b"elsewhere") and refused("figures/../../elsewhere.png", named[outside], png + b"elsewhere")
           and refused("figures/./headline.png", built[image]))
        ok("W45 it refuses a link and a file with a second hard link, each named with the hash of what it leads to",
           get(image, built[image])[0] == 200 and refused("figures/linked.png", built[image])
           and refused("figures/also_elsewhere.png", named["figures/also_elsewhere.png"], png + b"elsewhere"))
        ok("W46 it refuses a credential file name, although a view named it with its hash",
           get(image, built[image])[0] == 200 and all(refused(rel, sha, png + rel.encode()) for rel, sha in secret.items()))
        ok("W47 it refuses what is not an image (a table, a vector file, an artifact, a report), although a view named it with its hash",
           get(image, built[image])[0] == 200 and all(get(rel, sha)[0] != 200 for rel, sha in other.items()))
        ok("W48 it answers a loopback Host only, GET only, and a run the workbench found only",
           get(image, built[image])[0] == 200 and get(image, built[image], host="evil.example")[0] == 403
           and get(image, built[image], method="POST")[0] == 405 and get(image, built[image], run="nope")[0] == 404
           and wb.handle("GET", f"/api/file?run={rid}", "localhost")[0] != 200
           and wb.handle("GET", f"/api/file?run={rid}&path={image}", "localhost")[0] != 200)
        def snapshot():
            """Every path below the run folder now, with what it is: a file's hash, a link's target, or a folder."""
            out = {}
            for at, dirs, files in os.walk(d):
                for n in dirs + files:
                    full = os.path.join(at, n)
                    out[os.path.relpath(full, d)] = ("link " + os.readlink(full) if os.path.islink(full) else "folder" if os.path.isdir(full)
                                                     else hashlib.sha256(open(full, "rb").read()).hexdigest())
            return out

        def wrote_nothing(request):
            """request() answered 200, and the run folder is the same immediately before and after it."""
            before = snapshot()
            code = request()[0]
            return code == 200 and snapshot() == before

        def writing(what):
            def request():
                out = get(image, built[image])
                with open(os.path.join(d, what), "ab") as f:
                    f.write(b"written during the request")
                return out
            return request
        ok("W49 serving a file writes nothing: every path of the run folder is the same immediately before and after the request",
           wrote_nothing(lambda: get(image, built[image])))
        caught = [not wrote_nothing(writing("figures/timeline.csv")), not wrote_nothing(writing("figures/made_by_the_request.txt"))]
        os.remove(os.path.join(d, "figures", "made_by_the_request.txt"))
        put("figures/timeline.csv", _read_file(tmp, built, d, "figures/timeline.csv"))
        ok("W58 review 10: that comparison catches a request that answers 200 and changes a file, or makes a new one", caught == [True, True], str(caught))
        twin = os.path.realpath(os.path.join(tmp, "imaged", "twin"))
        shutil.copytree(d, twin, symlinks=True)
        with wb.lock:
            was = wb.runs[rid]
            wb.runs[rid], wb.found[twin] = twin, folder_id(twin)        # the id now leads to another folder with the same bytes
        elsewhere = get(image, built[image])[0]
        with wb.lock:
            wb.runs[rid] = was
        ok("W55 review 1: what a view of one run folder named is no permission in another folder that comes to have its id",
           elsewhere == 403 and get(image, built[image])[0] == 200, str(elsewhere))
        many = {f"figures/n{i}.png": hashlib.sha256(str(i).encode()).hexdigest() for i in range(NAMED_PAIRS + 50)}
        VIEW_FNS["fit"] = lambda run: {"checks": [], "facts": {}, "charts": [], "notes": {}, "images": declared, "tables": {"images": {
            "columns": ["figure", "path", "sha256", "title", "caption"], "rows": [[rel, rel, sha, None, None] for rel, sha in many.items()]}}}
        wb.api_view(rid, "fit")
        for i in range(NAMED_KEPT + 5):
            wb.named[("/nowhere", (0, i), "fit")] = set()
        wb.api_view(rid, "fit")
        pushed_out = get(image, built[image])[0]          # the view that named it was the oldest remembered
        wb.api_view(rid, "spec")
        ok("W59 review 1: the remembered set is bounded: so many view results, each so many images; a view pushed out is asked for again",
           len(wb.named) <= NAMED_KEPT and all(len(v) <= NAMED_PAIRS for v in wb.named.values()) and pushed_out == 403
           and get(image, built[image])[0] == 200, str((len(wb.named), max(len(v) for v in wb.named.values()), pushed_out)))
        riff = b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + bytes(28)
        ihdr = png[:8 + 25]                                    # the signature and the header chunk
        cut = {"figures/sound.webp": riff, "figures/sound.png": riff, "figures/signature_only.png": png[:8], "figures/header_only.png": ihdr,
               "figures/no_end.png": png[:-12], "figures/bad_crc.png": png[:29] + bytes([png[29] ^ 1]) + png[30:], "figures/tail.png": png + b"<script>",
               "figures/photo.jpg": b"\xff\xd8\xff\xe0" + bytes(16), "figures/anim.gif": b"GIF89a" + bytes(16)}
        shas = {rel: put(rel, data) for rel, data in cut.items()}
        VIEW_FNS["fit"] = lambda run: {"checks": [], "facts": {}, "charts": [], "notes": {}, "images": declared, "tables": {"images": {
            "columns": ["figure", "path", "sha256", "title", "caption"], "rows": [[rel, rel, sha, None, None] for rel, sha in shas.items()]}}}
        wb.api_view(rid, "fit")
        got = {rel: get(rel, sha) for rel, sha in shas.items()}
        ok("W56 review 2: a file is served only when it has the structure of the image its name says: a sound file named as an image, a "
           "signature with nothing after it, a header with no image data, no end, a damaged chunk and bytes after the end are all refused",
           all(code == 415 and (len(cut[rel]) <= 8 or cut[rel][8:] not in body) for rel, (code, _, body, _) in got.items()) and get(image, built[image])[0] == 200,
           str({rel: code for rel, (code, _, _, _) in got.items()}))
        slow = Workbench([], [d])
        slow.scan()
        slow.named = dict(wb.named)
        slow.file_wait = 0.01
        seen, target = [], "/api/file?" + urllib.parse.urlencode({"run": rid, "path": image, "sha": built[image]})

        def send(code, headers, body, depth=[0]):
            seen.append(code)
            if depth[0] < FILES_AT_ONCE:           # while this answer is still being written, another request arrives
                depth[0] += 1
                answer(slow, "GET", target, "127.0.0.1:8790", b"", {}, send)
        answer(slow, "GET", target, "127.0.0.1:8790", b"", {}, send)
        after = []
        answer(slow, "GET", target, "127.0.0.1:8790", b"", {}, lambda code, headers, body: after.append((code, headers.get("Content-Type"))))
        ok("W57 review 9: no more than FILES_AT_ONCE images are held at once, each until it is written; one more waits, then is told to ask again",
           seen == [200] * FILES_AT_ONCE + [503] and after == [(200, "image/png")], str((seen, after)))
        fake = put("figures/not_an_image.png", b"<svg xmlns='http://www.w3.org/2000/svg'><script>1</script></svg>")
        chart = {"kind": "bars", "table": "t", "x": "a", "y": "b", "title": "T", "how_to_read": "H"}
        VIEW_FNS["model"] = lambda run: {"tables": {"t": {"columns": ["a", "b"], "rows": [["x", 1]]}}, "charts": [
            dict(chart, image={"path": "figures/timeline.png", "hash": built["figures/timeline.png"], "title": "as drawn"}),
            dict(chart, image={"path": "figures/not_an_image.png", "hash": fake})]}
        before = refused("figures/timeline.png", built["figures/timeline.png"])
        doc = wb.api_view(rid, "model")
        ok("W50 a chart's `image` names a file too: it is served once the view that carries it has been sent, and not before",
           before and chart_problems(doc) == [] and get("figures/timeline.png", built["figures/timeline.png"])[:2] == (200, "image/png")
           and [len(chart_problems({**doc, "charts": [dict(chart, image=im)]})) for im in (
               {"path": "figures/timeline.png"}, {"path": "figures/timeline.png", "hash": "abc"}, "figures/timeline.png",
               {"path": "figures/timeline.png", "hash": "0" * 64, "width": 3})] == [1, 1, 1, 1], str(chart_problems(doc)))
        code, _, body, _ = get("figures/not_an_image.png", fake)
        ok("W51 a file named as an image that does not begin as one is refused, although a view named it with its hash",
           code == 415 and b"script" not in body, str((code, body)))
        codes = [get(image, built[image])[0], get(image, "0" * 64)[0], get("figures/timeline.csv", "0" * 64)[0], get("../elsewhere.png", named["../elsewhere.png"])[0],
                 get("figures/frontier.png", built["figures/frontier.png"])[0], get("figures/none.png", "0" * 64)[0], get(image, "abc")[0], get(image, built[image], run="nope")[0]]
        ok("W52 each refusal has its own answer and a reason in plain text: 403 not named or outside the run, 409 the file changed, 415 not a "
           "raster image, 400 no hash, 404 no such run", codes == [200, 403, 415, 403, 409, 403, 400, 404]
           and get(image, "0" * 64)[2].startswith(b"not served: ") and get(image, "0" * 64)[1].startswith("text/plain"), str(codes))
        sent, page = response_headers("image/png", 3, get(image, built[image])[3]), response_headers("text/html", 3)
        ok("W53 the handler sends an image under its own policy and the page under the page's; nothing is stored, and no type is guessed",
           sent["Content-Security-Policy"] == FILE_CSP and page["Content-Security-Policy"] == CSP and "img-src 'self'" in CSP
           and all(h["Cache-Control"] == "no-store" and h["X-Content-Type-Options"] == "nosniff" for h in (sent, page))
           and sent["Content-Type"] == "image/png" and sent["Content-Length"] == "3", str(sent))
        good = {"images": declared, "tables": {"images": {"columns": ["figure", "path", "sha256", "title", "caption"], "rows": []}}}
        script = open(os.path.join(STATIC, "app.js")).read()
        ok("W54 image declarations: every documented key is read by the page; an unknown key, table or column is reported; "
           "the Figures view's own declaration on a fixture run is one the page can draw",
           image_problems(good) == [] and [len(image_problems({**good, "images": [dict(declared[0], **e)]})) for e in (
               {"colour": "red"}, {"table": "none"}, {"hash": "z"}, {"group": "z"}, {"details": ["figure", "z"]}, {"details": "figure"},
               {"flag": "z"}, {"reason": "z"}, {"after": "none"})] == [1] * 9 and image_problems({**good, "images": [dict(declared[0], after="images", flag="title", reason="caption")]}) == []
           and len(chart_problems({"tables": {"t": {"columns": ["a", "b"], "rows": []}}, "charts": [{"kind": "bars", "table": "t", "x": "a", "y": "b", "title": "T", "how_to_read": "H", "after": "none"}]})) == 1
           and all(re.search(rf"\bd\.{k}\b|\b(at\(|text\(r, )\"{k}\"\)", script) for k in IMAGE_KEYS) and all(k in __doc__ for k in IMAGE_KEYS + ("IMAGE_KEYS", "FILE_TYPES", "FILE_CSP"))
           and all(re.search(rf"\bim\.{k}\b", script) for k in CHART_IMAGE_KEYS)
           and image_problems(wb.api_view(rid, "figures")) == [] and len(named_images(wb.api_view(rid, "figures"))) > 3
           and wb.api_view(rid, "figures")["images"][0]["table"] == "images", str(image_problems(wb.api_view(rid, "figures"))))
    finally:
        for name in ("spec", "model", "fit"):
            VIEW_FNS.pop(name, None)


def _self_test_against(tmp, ok):
    """The comparison run of the Outcomes view (§5.7): `GET /api/view?run=<id>&view=outcomes&against=<run id>` gives
    the view the other run as its second argument, `view(run, against=None)`. These cases fail until the server reads
    the parameter; they travel with the view's code (§9)."""
    from views.fit_bench import fixture_builder
    one, two = os.path.join(tmp, "against", "one"), os.path.join(tmp, "against", "two")
    fixture_builder().build(one)
    fixture_builder().build(two, other="second")
    wb = Workbench([], [one, two])
    ids = {os.path.basename(p): k for k, p in wb.scan().items()}

    def seen(run, against=None):
        return {"facts": {"run": run.path, "against": None if against is None else against.path}}

    def get(view, **more):
        q = urllib.parse.urlencode({"run": ids["one"], "view": view, **more})
        code, _, body = wb.handle("GET", f"/api/view?{q}", "127.0.0.1")
        return code, (json.loads(body) if code == 200 else body.decode())
    VIEW_FNS.update(outcomes=seen, spec=lambda run: {"facts": {"run": run.path}})
    try:
        alone, both = get("outcomes"), get("outcomes", against=ids["two"])
        ok("W55 `&against=<run id>` gives the view the other run as its second argument; with no such parameter the view is given none",
           alone[0] == both[0] == 200 and alone[1]["facts"] == {"run": os.path.realpath(one), "against": None}
           and both[1]["facts"] == {"run": os.path.realpath(one), "against": os.path.realpath(two)}, str((alone, both))[:300])
        again = get("outcomes")
        ok("W56 the answer with a comparison is not kept as the answer for the run alone, nor the other way round",
           again[0] == 200 and again[1]["facts"]["against"] is None
           and get("outcomes", against=ids["two"])[1]["facts"].get("against") == os.path.realpath(two), str(again)[:300])
        missing = get("outcomes", against="no-such-run")
        ok("W57 an `against` that names no run is a 404 that says so: a comparison is never made with nothing, and never "
           "answered as the run alone",
           missing[0] == 404 and "against" in missing[1], str(missing)[:300])
        plain = get("spec", against=ids["two"])
        ok("W58 a view that takes the run alone is called with the run alone, whatever the address holds",
           plain[0] == 200 and plain[1]["facts"] == {"run": os.path.realpath(one)}, str(plain)[:300])
        outside = get("outcomes", against="../two")
        ok("W59 `against` is a run id of this server's list, never a path", outside[0] == 404, str(outside)[:300])
    finally:
        for name in ("outcomes", "spec"):
            VIEW_FNS.pop(name, None)


def _self_test_notes(tmp, ok):
    """The one write (§6 "Review notes"): POST /api/note appends one line to <run>/owner/review_notes.jsonl."""
    import review_notes
    from views.fit_bench import fixture_builder
    d = os.path.join(tmp, "noted", "run")
    fixture_builder().build(d, defect="C12_open_high")
    wb, rel = Workbench([], [d]), review_notes.FILE
    rid = next(iter(wb.scan()))
    file = os.path.join(d, rel)

    def post(body, token=None, host="127.0.0.1:8790", run=rid, path="/api/note", **headers):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return wb.handle("POST", f"{path}?run={urllib.parse.quote(run)}", host, raw,
                         {"x-workbench-token": wb.token if token is None else token, "content-type": "application/json", **headers})

    def tree(skip=(rel,)):
        out = {}
        for root, _, names in os.walk(d):
            for n in names:
                r = os.path.relpath(os.path.join(root, n), d)
                if r not in skip:
                    out[r] = hashlib.sha256(open(os.path.join(root, n), "rb").read()).hexdigest()
        return out

    def lines():
        return open(file, "rb").read().split(b"\n")[:-1] if os.path.exists(file) else []

    def row(rid_):
        t = wb.api_view(rid, "register")["tables"]["rows"]
        return next((dict(zip(t["columns"], r)) for r in t["rows"] if r[0] == rid_), None)
    before = tree()
    page = wb.handle("GET", "/", "localhost")[2].decode()
    static = open(os.path.join(STATIC, "index.html")).read()
    ok("N1 the token is made at server start and put in the page; the static file holds none, and two servers differ",
       len(wb.token) >= 32 and f'content="{wb.token}"' in page and wb.token not in static and TOKEN_MARK in static
       and Workbench([], [d]).token != wb.token)
    good = {"row": "D-001", "disposition": "declared", "note": "accepted as a stated limit"}
    refused = [post(good, token=""), post(good, token="x" * len(wb.token)), post(good, token=wb.token + "x"),
               wb.handle("POST", f"/api/note?run={rid}", "127.0.0.1", json.dumps(good).encode(), {})]
    ok("N2 a POST with no token or a wrong one is refused (403) and writes nothing",
       [r[0] for r in refused] == [403] * 4 and not os.path.exists(os.path.dirname(file)), str([r[:1] for r in refused]))
    away = [post(good, host="evil.example"), post(good, host="127.0.0.1.evil.example:8790"), post(good, host=""),
            post(good, origin="http://evil.example"), post(good, origin="null"),
            post(good, origin="http://127.0.0.1:9999"), post(good, origin="http://localhost:8790"),
            post(good, origin="https://127.0.0.1:8790"), post(good, origin="http://127.0.0.1")]
    ok("N3 a POST whose Host is not loopback, or that comes from another origin (a remote one, another loopback port or "
       "name, another scheme), is refused (403) and writes nothing",
       [r[0] for r in away] == [403] * 9 and not os.path.exists(os.path.dirname(file)), str([r[0] for r in away]))
    bad = [b"not json", b"[1, 2]", {**good, "disposition": "closed"}, {**good, "file": "../../elsewhere.jsonl"},
           {**good, "at": "2020-01-01T00:00:00+00:00"}, {"row": "Z-999", "disposition": "declared"}, {"row": "D-001"},
           {"row": "D-001", "same_as": "Z-999"}, {"row": "D-001", "same_as": "D-001"}, {"row": None, "kind": "issue"},
           {"row": None, "statement": "s"}, {"row": "D-001", "statement": "restated", "note": "x"}, {"row": 7, "note": "x"},
           {"row": "D-001", "note": {"a": 1}}, b"\xff\xfe"]
    got = [post(x)[0] for x in bad]
    ok("N4 a note outside the schema is refused (400) with the reason and writes nothing: not JSON, an unknown key or "
       "word, a row the register does not hold, a note that says nothing, a time or a file name from the client",
       got == [400] * len(bad) and not os.path.exists(os.path.dirname(file)) and b"closed" in post(bad[2])[2], str(got))
    lost = [post(good, run="nope"), post(good, run=d), post(good, run="../run"), post(good, path="/api/runs"),
            post(good, path="/api/view"), wb.handle("PUT", f"/api/note?run={rid}", "localhost", b"{}", {"x-workbench-token": wb.token}),
            post(good, **{"content-type": "text/plain"}), post(b" " * (NOTE_BODY + 1))]
    ok("N5 a run that is not one the workbench found (an unknown id, a path), another address, another method, another "
       "content type or an over-long body is refused, and nothing is written",
       [r[0] for r in lost] == [404, 404, 404, 405, 405, 405, 415, 413] and not os.path.exists(os.path.dirname(file)), str([r[0] for r in lost]))
    first = post(good, origin="http://127.0.0.1:8790")        # the page's own origin: this server's scheme and authority
    one = lines()
    ok("N6 a good note is appended as one line with the server's time, and the register shows it beside the derived value",
       first[0] == 200 and len(one) == 1 and json.loads(one[0])["row"] == "D-001" and _ts(json.loads(one[0])["at"]) is not None
       and sorted(json.loads(one[0])) == sorted(review_notes.KEYS)
       and (row("D-001")["disposition"], row("D-001")["signed_by"]) == ("declared", "owner")
       and wb.api_view(rid, "register")["tables"]["notes"]["rows"][0][2:4] == ["declared", "open"], str((first, one)))
    crafted = 'x"}\n{"at": "2026-01-02T09:00:00+00:00", "row": "D-002", "kind": null, "statement": null, ' \
              '"disposition": "withdrawn", "decides": null, "note": ""}\r\n'
    second = post({"row": "D-002", "note": crafted, "decides": "no"})
    two = lines()
    ok("N7 append, not overwrite: the first line is unchanged; a note whose text holds a line break and a JSON object "
       "is still one line and changes no other row",
       second[0] == 200 and len(two) == 2 and two[0] == one[0] and json.loads(two[1])["note"] == crafted
       and row("D-002")["disposition"] == "implemented" and row("D-002")["decides"] == "no", str((second, len(two))))
    third = post({"row": None, "kind": "uncertainty", "statement": "The two rounds may not share a sampling frame."})
    fourth = post({"row": "note:1", "same_as": "D-002"})
    ok("N8 a new row and a same-as note: the row is added as note:1, and the row named second is folded into it",
       third[0] == 200 and fourth[0] == 200 and len(lines()) == 4 and row("D-002") is None
       and "findings" in row("note:1")["source"] and wb.api_view(rid, "register")["facts"]["n_owner_notes"] == 4, str((third, fourth)))
    ok("N9 nothing else is ever written: every other file of the run is as it was, and the owner folder holds that one file",
       tree() == before and os.listdir(os.path.join(d, "owner")) == [os.path.basename(rel)], str(set(tree()) ^ set(before)))
    elsewhere = os.path.join(tmp, "noted", "elsewhere")
    os.makedirs(elsewhere)
    outside = os.path.join(elsewhere, os.path.basename(rel))
    kept = open(file, "rb").read()
    remove_nofollow(d, rel)
    os.symlink(outside, file)           # lib.safe_io makes no link: the three planted ones are plain os calls
    a = post(good)
    remove_nofollow(d, rel)
    os.rmdir(os.path.join(d, "owner"))
    os.symlink(elsewhere, os.path.join(d, "owner"))
    b = post(good)
    remove_nofollow(d, "owner")
    os.makedirs(os.path.join(d, "owner"))
    write_nofollow(elsewhere, os.path.basename(rel), kept)
    os.link(outside, file)
    c = post(good)
    ok("N10 a note is written nowhere but that file: a notes file that is a link, an owner folder that is a link and a "
       "notes file also linked from elsewhere are each refused, and the file elsewhere is as it was",
       [a[0], b[0], c[0]] == [409] * 3 and open(outside, "rb").read() == kept and os.listdir(elsewhere) == [os.path.basename(rel)],
       str([a, b, c]))
    doc = {"tables": {"t": {"columns": ["id", "x"], "rows": [], "actions": {
        "post": "/api/note", "key": "id", "label": "Review", "about": "A",
        "fields": [{"name": "disposition", "label": "D", "choices": ["a"]}, {"name": "same_as", "label": "S", "rows": True},
                   {"name": "note", "label": "N", "text": True, "required": True}],
        "add": {"label": "Add", "fields": [{"name": "statement", "label": "S", "text": True}]}}}}}

    def wrong(**edit):
        t = json.loads(json.dumps(doc))
        t["tables"]["t"]["actions"].update(edit)
        return action_problems(t)
    script = open(os.path.join(STATIC, "app.js")).read()
    ok("N11 action declarations: every documented key is read by the page; an unknown address, key or column, and a "
       "field that is not one of choices, rows or text, are each reported",
       action_problems(doc) == [] and all(len(wrong(**e)) == 1 for e in (
           {"post": "/api/runs"}, {"key": "z"}, {"colour": "red"}, {"label": ""},
           {"fields": [{"name": "n", "label": "L"}]}, {"fields": [{"name": "n", "label": "L", "text": True, "size": 3}]},
           {"add": {"label": "Add", "fields": [{"label": "L", "text": True}]}}))
       and all(re.search(rf"\b(a|f|actions|add)\.{k}\b", script) for k in ACTION_KEYS + FIELD_KEYS)
       and all(k in __doc__ for k in ACTION_KEYS + FIELD_KEYS + ("lead", "actions", "wrap"))
       and all(re.search(rf"\bt\.{k}\b", script) for k in ("lead", "actions", "wrap")),
       str([k for k in ACTION_KEYS + FIELD_KEYS if not re.search(rf"\b(a|f|actions|add)\.{k}\b", script)]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--roots", nargs="*", default=None, help="folders to search for runs (default: runs/, unless --run-dir is given)")
    ap.add_argument("--run-dir", action="append", default=[])
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--retro", metavar="RUN_DIR")
    ap.add_argument("--accept", nargs=2, metavar=("RUN_DIR", "ACCEPTANCE_YAML"))
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if a.retro:
        return retro(a.retro)
    if a.accept:
        return accept(Workbench([], [a.accept[0], *a.run_dir]), *a.accept)
    serve(Workbench(chosen_roots(a.roots, a.run_dir), a.run_dir), a.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
