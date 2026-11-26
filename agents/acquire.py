#!/usr/bin/env python3
"""Harness-controlled public data acquisition for v2 ACQUIRE (docs/design/acquire.md §§3.2, 4, 7).

    python3 agents/acquire.py --self-test

Allowlist-only, SSRF-pinned HTTPS fetcher, quarantine and nine-step promoter, sanitized view generator,
and tamper-evident inventory verifier (`verify_inventory`). Every filesystem operation inside a run
directory uses descriptor-relative `scripts/lib/safe_io.py` helpers (no symlink following). Nothing
here names a disease, country, or subnational unit.
"""
from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import http.client
import http.server
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as _ET
import xml.parsers.expat as _expat
from collections.abc import Callable, Mapping

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
from lib import safe_io as SIO  # noqa: E402

DEFAULT_ALLOWLIST = os.path.join(REPO, ".claude", "orchestration", "acquire_sources.yaml")
ACQUIRE_DIR = "acquire"
QUARANTINE_DIR = "acquire/quarantine"
RECORD_REL = "acquire/record.json"
MANIFEST_REL = "evidence/acquired_manifest.json"
PROVENANCE_REL = "data/inputs_provenance.json"
ACQUIRED_ROOT = "data/inputs/acquired"
VIEWS_ROOT = "data/inputs/acquired/_views"

MAX_FETCHES_PER_ROUND = 40
MAX_FETCHES_PER_RUN = 80
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_RUN_BYTES = 200 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_QUERY_CHARS = 512
MAX_TERMS_CHARS = 120
MAX_TERMS_WORDS = 8
MAX_PREVIEW_BYTES = 4096
CONNECT_TIMEOUT_S = 20.0
TOTAL_TIMEOUT_S = 120.0

VALID_STATUSES = ("enabled", "excluded")
VALID_REDISTRIBUTION = ("allowed", "not_allowed", "per_record")
VALID_PARAM_TYPES = ("code", "enum", "int_range", "pattern", "terms")
ALLOWED_TYPES_V1 = frozenset({
    "text/csv", "application/json", "application/xml", "text/xml", "text/plain",
})
TABULAR_TYPES = frozenset({"text/csv", "application/json"})
TEXT_TYPES = frozenset({"application/xml", "text/xml", "text/plain"})
EXT_FOR_TYPE = {
    "text/csv": "csv",
    "application/json": "json",
    "application/xml": "xml",
    "text/xml": "xml",
    "text/plain": "txt",
}
_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9_ .\-]")
_TERMS_RE = re.compile(r"^[A-Za-z0-9 ]+$")
_URL_LIKE_RE = re.compile(r"(?i)(?:https?://|ftp://|file://|://|\.\.|[\r\n\0])")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_.-]+)\}")


class AcquireError(RuntimeError):
    """Base error for fail-closed acquisition checks."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


class AllowlistError(AcquireError):
    """Allowlist failed schema or licence completeness validation."""


class FetchRefused(AcquireError):
    """Fetch request was refused before reading response bytes."""


class ArtifactTampered(AcquireError):
    """Quarantined or promoted acquired bytes failed integrity verification (terminal)."""


# --------------------------------------------------------------------------- Allowlist loader (§7, AQ1)
def load_allowlist(path: str = DEFAULT_ALLOWLIST) -> tuple[dict, str]:
    """Load and validate `.claude/orchestration/acquire_sources.yaml` (fail closed; AQ1)."""
    try:
        with open(path, "rb") as f:
            raw_bytes = f.read()
        doc = yaml.safe_load(raw_bytes)
    except (OSError, yaml.YAMLError) as e:
        raise AllowlistError("allowlist_unreadable", f"{path}: {e}") from None
    if not isinstance(doc, Mapping):
        raise AllowlistError("allowlist_invalid", "top-level mapping required")
    ver = doc.get("version")
    if isinstance(ver, bool) or not isinstance(ver, int) or ver < 1:
        raise AllowlistError("allowlist_invalid", "integer version >= 1 required")
    wk = doc.get("wanted_kinds")
    if not isinstance(wk, list) or not wk or not all(isinstance(x, str) and x for x in wk):
        raise AllowlistError("allowlist_invalid", "non-empty wanted_kinds list required")
    wk_set = set(wk)
    acc = doc.get("accepted_licences")
    if not isinstance(acc, list) or not acc or not all(isinstance(x, str) and x for x in acc):
        raise AllowlistError("allowlist_invalid", "non-empty accepted_licences list required")
    sources = doc.get("sources")
    if not isinstance(sources, list) or not sources:
        raise AllowlistError("allowlist_invalid", "non-empty sources list required")

    by_id: dict[str, dict] = {}
    for i, src in enumerate(sources):
        if not isinstance(src, Mapping):
            raise AllowlistError("allowlist_invalid", f"sources[{i}] must be a mapping")
        sid = str(src.get("id") or "")
        if not sid or not re.fullmatch(r"[a-z0-9_]+", sid):
            raise AllowlistError("allowlist_invalid", f"sources[{i}].id invalid: {sid!r}")
        if sid in by_id:
            raise AllowlistError("allowlist_invalid", f"duplicate source id {sid!r}")
        if not str(src.get("holds") or "").strip():
            raise AllowlistError("allowlist_invalid", f"{sid}: holds description required")
        st = src.get("status")
        if st not in VALID_STATUSES:
            raise AllowlistError("allowlist_invalid", f"{sid}: status must be one of {VALID_STATUSES}")
        if st == "excluded" and not str(src.get("reason") or "").strip():
            raise AllowlistError("allowlist_invalid", f"{sid}: excluded source requires reason")
        if not isinstance(src.get("requires_key"), bool):
            raise AllowlistError("allowlist_invalid", f"{sid}: boolean requires_key required")
        rl = src.get("rate_limit")
        if not isinstance(rl, Mapping) or isinstance(rl.get("rps"), bool) or not isinstance(rl.get("rps"), (int, float)) or float(rl["rps"]) <= 0:
            raise AllowlistError("allowlist_invalid", f"{sid}: rate_limit.rps > 0 required")
        lic = src.get("licence")
        if not isinstance(lic, Mapping):
            raise AllowlistError("allowlist_invalid", f"{sid}: licence mapping required")
        for req_k in ("id", "terms_url", "checked"):
            if not str(lic.get(req_k) or "").strip():
                raise AllowlistError("allowlist_invalid", f"{sid}: licence.{req_k} required")
        if not str(lic["terms_url"]).startswith(("https://", "http://")):
            raise AllowlistError("allowlist_invalid", f"{sid}: licence.terms_url must be a URL")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(lic["checked"])):
            raise AllowlistError("allowlist_invalid", f"{sid}: licence.checked must be YYYY-MM-DD")
        redist = lic.get("redistribution")
        if redist not in VALID_REDISTRIBUTION:
            raise AllowlistError("allowlist_invalid", f"{sid}: licence.redistribution must be in {VALID_REDISTRIBUTION}")
        if redist == "per_record":
            arl = lic.get("accepted_record_licences")
            if not isinstance(arl, list) or not arl:
                raise AllowlistError("allowlist_invalid", f"{sid}: per_record redistribution requires accepted_record_licences")

        eps = src.get("endpoints")
        if not isinstance(eps, Mapping):
            raise AllowlistError("allowlist_invalid", f"{sid}: endpoints mapping required")
        if st == "enabled" and not eps:
            raise AllowlistError("allowlist_invalid", f"{sid}: enabled source requires at least one endpoint")
        for ename, ep in eps.items():
            if not isinstance(ep, Mapping):
                raise AllowlistError("allowlist_invalid", f"{sid}.{ename}: endpoint must be a mapping")
            host = str(ep.get("host") or "")
            pt = str(ep.get("path_template") or "")
            if not host or "/" in host or ":" in host:
                raise AllowlistError("allowlist_invalid", f"{sid}.{ename}: plain host required")
            if not pt.startswith("/"):
                raise AllowlistError("allowlist_invalid", f"{sid}.{ename}: path_template must start with '/'")
            acts = ep.get("allowed_content_types")
            if not isinstance(acts, list) or not acts or not set(acts) <= ALLOWED_TYPES_V1:
                raise AllowlistError("allowlist_invalid", f"{sid}.{ename}: allowed_content_types must be subset of {sorted(ALLOWED_TYPES_V1)}")
            ewk = ep.get("wanted_kinds")
            if not isinstance(ewk, list) or not ewk or not set(ewk) <= wk_set:
                raise AllowlistError("allowlist_invalid", f"{sid}.{ename}: wanted_kinds must be subset of {wk}")
            params = ep.get("params")
            if not isinstance(params, Mapping):
                raise AllowlistError("allowlist_invalid", f"{sid}.{ename}: typed params mapping required")
            for pname, pspec in params.items():
                if not isinstance(pspec, Mapping) or pspec.get("type") not in VALID_PARAM_TYPES:
                    raise AllowlistError("allowlist_invalid", f"{sid}.{ename}.params.{pname}: valid type in {VALID_PARAM_TYPES} required")
                ptype = pspec["type"]
                if ptype in ("code", "pattern") and not str(pspec.get("pattern") or ""):
                    raise AllowlistError("allowlist_invalid", f"{sid}.{ename}.params.{pname}: pattern required")
                if ptype == "enum" and (not isinstance(pspec.get("values"), list) or not pspec["values"]):
                    raise AllowlistError("allowlist_invalid", f"{sid}.{ename}.params.{pname}: enum values list required")
                if ptype == "int_range" and (pspec.get("min") is None or pspec.get("max") is None):
                    raise AllowlistError("allowlist_invalid", f"{sid}.{ename}.params.{pname}: int_range min/max required")
            for ph in _PLACEHOLDER_RE.findall(pt):
                if ph not in params:
                    raise AllowlistError("allowlist_invalid", f"{sid}.{ename}: path placeholder {{{ph}}} missing from params")
        by_id[sid] = dict(src)

    norm = dict(doc)
    norm["_by_id"] = by_id
    return norm, hashlib.sha256(raw_bytes).hexdigest()


# --------------------------------------------------------------------------- Safe text, XML & parsing helpers
def sanitize_label(text: str, *, max_len: int = 64, fallback: str = "col") -> str:
    """Filter untrusted metadata/column labels to `[A-Za-z0-9_ .-]` <= `max_len` chars (§3.3)."""
    cleaned = _SAFE_LABEL_RE.sub("", str(text or "")).strip()[:max_len]
    return cleaned if cleaned else fallback


def sanitize_preview(raw: bytes, *, max_bytes: int = MAX_PREVIEW_BYTES) -> str:
    """Control-character-stripped preview capped at `max_bytes` bytes (§3.3)."""
    text = raw[:max_bytes].decode("utf-8", errors="replace")
    return "".join(ch for ch in text if ch in ("\n", "\t") or (ord(ch) >= 32 and ord(ch) != 127))


def _parse_xml_safe(raw: bytes) -> _ET.Element:
    """Parse XML strictly refusing any DOCTYPE, ENTITY, or external reference (XXE / billion-laughs guard; AQ8)."""
    try:
        import defusedxml.ElementTree as _defused_et
        return _defused_et.fromstring(raw)
    except ImportError:
        pass
    # Fail-closed expat pre-validation when defusedxml is absent: reject any DTD / entity declaration
    parser = _expat.ParserCreate()

    def _forbid(*_args, **_kw):
        raise ValueError("XML DTD/entity declarations are forbidden")

    parser.StartDoctypeDeclHandler = _forbid
    parser.EntityDeclHandler = _forbid
    parser.UnparsedEntityDeclHandler = _forbid
    parser.NotationDeclHandler = _forbid
    parser.ExternalEntityRefHandler = _forbid
    parser.Parse(raw, True)
    return _ET.fromstring(raw)


def extract_plain_text(raw: bytes, content_type: str) -> str:
    """Harness-extracted plain text from an acquired literature file (for §5 quote checks)."""
    ct = content_type.split(";")[0].strip().lower()
    if ct in ("application/xml", "text/xml"):
        root = _parse_xml_safe(raw)
        return " ".join(s.strip() for s in root.itertext() if s and s.strip())
    return raw.decode("utf-8", errors="replace")


def sniff_content_type(raw: bytes, header_ct: str) -> str | None:
    """Sniff bytes and require agreement with declared content type (AQ7: rejects HTML-as-JSON, binaries, PDFs)."""
    declared = (header_ct or "").split(";")[0].strip().lower()
    if declared not in ALLOWED_TYPES_V1:
        return None
    if not raw or b"\x00" in raw[:4096]:
        return None
    head = raw.lstrip()[:512]
    head_l = head.lower()
    # Explicitly refuse PDF, archive magics, and HTML error pages
    if head.startswith((b"%PDF", b"PK\x03\x04", b"\x1f\x8b", b"BZh", b"\xfd7zXZ")):
        return None
    if head_l.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<!--")) or b"<html" in head_l[:256]:
        return None
    if declared == "application/json":
        return declared if head[:1] in (b"{", b"[") else None
    if declared in ("application/xml", "text/xml"):
        return declared if head[:1] == b"<" else None
    if declared == "text/csv":
        if head[:1] in (b"{", b"[", b"<"):
            return None
        return declared
    if declared == "text/plain":
        return declared
    return None


def validate_and_parse(raw: bytes, content_type: str) -> tuple[bool, object, str]:
    """Step 4 (§4): verify bytes parse cleanly under their declared type (never converts bytes; AQ8)."""
    ct = content_type.split(";")[0].strip().lower()
    try:
        if ct == "application/json":
            return True, json.loads(raw.decode("utf-8")), ""
        if ct == "text/csv":
            text = raw.decode("utf-8")
            rows = list(csv.reader(io.StringIO(text)))
            if len(rows) < 2 or not rows[0] or all(not c.strip() for c in rows[0]):
                return False, None, "CSV requires a non-empty header and at least one data row"
            width = len(rows[0])
            for idx, r in enumerate(rows[1:], start=2):
                if len(r) != width:
                    return False, None, f"CSV row {idx} has {len(r)} columns, expected {width}"
            return True, rows, ""
        if ct in ("application/xml", "text/xml"):
            return True, _parse_xml_safe(raw), ""
        if ct == "text/plain":
            text = raw.decode("utf-8")
            if not text.strip():
                return False, None, "empty text payload"
            return True, text, ""
    except Exception as e:  # noqa: BLE001 — fail closed on any parse error
        return False, None, f"{type(e).__name__}: {e}"
    return False, None, f"unsupported content type {ct!r}"


def _extract_record_licences(parsed: object, content_type: str) -> list[str | None]:
    """Extract per-record licence strings from a JSON or XML payload (AQ11)."""
    ct = content_type.split(";")[0].strip().lower()
    if ct == "application/json":
        records: list[Mapping] = []
        if isinstance(parsed, Mapping):
            for list_key in ("records", "results", "resultList", "items", "data", "value"):
                val = parsed.get(list_key)
                if isinstance(val, Mapping) and isinstance(val.get("result"), list):
                    records = [r for r in val["result"] if isinstance(r, Mapping)]
                    break
                if isinstance(val, list):
                    records = [r for r in val if isinstance(r, Mapping)]
                    break
            if not records:
                records = [parsed]
        elif isinstance(parsed, list):
            records = [r for r in parsed if isinstance(r, Mapping)]
        if not records:
            return [None]
        out: list[str | None] = []
        for r in records:
            lv = r.get("licence") or r.get("license") or r.get("licenseCode")
            out.append(str(lv).strip() if isinstance(lv, str) and lv.strip() else None)
        return out
    if ct in ("application/xml", "text/xml") and isinstance(parsed, _ET.Element):
        # Look for <record> / <article> containers or top-level <license> / <licence>
        recs = list(parsed.findall(".//article")) or list(parsed.findall(".//record")) or [parsed]
        out_xml: list[str | None] = []
        for rec in recs:
            lic_val = None
            for elem in rec.iter():
                tag = elem.tag.split("}")[-1].lower()
                if tag in ("license", "licence"):
                    cand = elem.attrib.get("license-type") or elem.attrib.get("type") or (elem.text or "").strip()
                    if cand:
                        lic_val = cand.strip()
                        break
            out_xml.append(lic_val)
        return out_xml
    return [None]


def check_licence(src: Mapping, allowlist: Mapping, parsed: object, content_type: str) -> tuple[bool, list[str], str]:
    """Step 5 (§4, AQ11): verify source licence and, for `per_record`, EVERY record's licence."""
    accepted = {str(x).strip().lower() for x in allowlist["accepted_licences"]}
    lic = src["licence"]
    if str(lic["id"]).strip().lower() not in accepted:
        return False, [], f"source licence {lic['id']!r} not in accepted_licences"
    if lic["redistribution"] != "per_record":
        return True, [], ""
    rec_acc = {str(x).strip().lower() for x in (lic.get("accepted_record_licences") or [])}
    rec_lics = _extract_record_licences(parsed, content_type)
    if not rec_lics:
        return False, [], "per-record licence response contained no records"
    bad = [x for x in rec_lics if x is None or x.lower() not in rec_acc]
    if bad:
        return False, [x or "" for x in rec_lics], f"unaccepted or missing record licence(s): {bad[:3]}"
    return True, [str(x) for x in rec_lics], ""


def build_schema_view(raw: bytes, content_type: str, digest: str, source_id: str, endpoint: str) -> dict:
    """Build sanitized schema view for `draft_spec` (§3.3): NO free-text cells ever emitted."""
    ct = content_type.split(";")[0].strip().lower()
    view: dict = {
        "id": digest,
        "source_id": source_id,
        "endpoint": endpoint,
        "content_type": ct,
        "bytes": len(raw),
    }
    if ct == "text/csv":
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8"))))
        hdr = rows[0]
        data = rows[1:]
        cols = []
        for idx, raw_name in enumerate(hdr):
            cname = sanitize_label(raw_name, max_len=64, fallback=f"col_{idx}")
            vals = [r[idx].strip() for r in data if idx < len(r) and r[idx].strip() != ""]
            nums: list[float] = []
            for v in vals:
                try:
                    nums.append(float(v))
                except ValueError:
                    nums = []
                    break
            if nums and len(nums) == len(vals):
                cols.append({"name": cname, "type": "number", "min": min(nums), "max": max(nums),
                             "non_empty": len(vals)})
            else:
                cols.append({"name": cname, "type": "string", "distinct": len(set(vals)), "non_empty": len(vals)})
        view["row_count"] = len(data)
        view["columns"] = cols
    elif ct == "application/json":
        parsed = json.loads(raw.decode("utf-8"))
        records: list[Mapping] = []
        if isinstance(parsed, list):
            if len(parsed) == 2 and isinstance(parsed[1], list):  # World Bank [meta, rows]
                records = [r for r in parsed[1] if isinstance(r, Mapping)]
            else:
                records = [r for r in parsed if isinstance(r, Mapping)]
        elif isinstance(parsed, Mapping):
            for k in ("value", "Data", "data", "results", "studies", "records"):
                v = parsed.get(k)
                if isinstance(v, list):
                    records = [r for r in v if isinstance(r, Mapping)]
                    break
            if not records:
                records = [parsed]
        keys = sorted({str(k) for r in records[:500] for k in r.keys()})
        cols = []
        for idx, raw_k in enumerate(keys):
            cname = sanitize_label(raw_k, max_len=64, fallback=f"col_{idx}")
            vals = [r.get(raw_k) for r in records if r.get(raw_k) is not None]
            if vals and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
                fvals = [float(v) for v in vals]
                cols.append({"name": cname, "type": "number", "min": min(fvals), "max": max(fvals),
                             "non_empty": len(vals)})
            else:
                distinct = len({json.dumps(v, sort_keys=True, default=str) for v in vals})
                cols.append({"name": cname, "type": "categorical_or_nested", "distinct": distinct,
                             "non_empty": len(vals)})
        view["row_count"] = len(records)
        view["columns"] = cols
    else:
        view["kind"] = "literature_text"
    return view


# --------------------------------------------------------------------------- SSRF-pinned HTTP(S) client (§3.2)
def _is_public_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return bool(
        addr.is_global
        and not addr.is_private
        and not addr.is_loopback
        and not addr.is_link_local
        and not addr.is_multicast
        and not addr.is_reserved
        and not addr.is_unspecified
    )


def _default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects TCP strictly to `pinned_ip` while verifying TLS certificates against `server_hostname` (AQ4)."""

    def __init__(self, host: str, port: int, *, pinned_ip: str, timeout: float, context: ssl.SSLContext):
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw_sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw_sock, server_hostname=self.host)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Test-only HTTP connection pinned to `pinned_ip` (never used unless `_allow_test_http=True`)."""

    def __init__(self, host: str, port: int, *, pinned_ip: str, timeout: float):
        super().__init__(host, port=port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


def _template_path_regex(path_template: str, params_schema: Mapping) -> re.Pattern:
    """Compile an exact regex matching paths produced by `path_template` with its typed placeholders."""
    parts: list[str] = []
    pos = 0
    for m in _PLACEHOLDER_RE.finditer(path_template):
        parts.append(re.escape(path_template[pos:m.start()]))
        pname = m.group(1)
        pspec = params_schema[pname]
        pat = pspec.get("pattern") or r"[A-Za-z0-9_.\-]+"
        pat = pat.lstrip("^").rstrip("$")
        parts.append(f"(?:{pat})")
        pos = m.end()
    parts.append(re.escape(path_template[pos:]))
    return re.compile("^" + "".join(parts) + "$")


def _extract_catalogue_codes(raw: bytes, content_type: str, field: str) -> set[str]:
    """Extract coded identifiers from a catalogue or search response for AQ2b binding."""
    ct = content_type.split(";")[0].strip().lower()
    found: set[str] = set()
    if ct != "application/json":
        return found
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return found

    def _walk(node):
        if isinstance(node, Mapping):
            if field in node:
                val = node[field]
                if isinstance(val, str) and val.strip():
                    found.add(val.strip())
                elif isinstance(val, list):
                    for x in val:
                        if isinstance(x, str) and x.strip():
                            found.add(x.strip())
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(obj)
    return found


def _canonical_digest(source_id: str, endpoint: str, params: Mapping, allowlist_sha: str) -> str:
    payload = json.dumps(
        [source_id, endpoint, sorted((str(k), str(v)) for k, v in params.items()), allowlist_sha],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------- Record persistence (safe_io only)
def _load_record(run_dir: str, allowlist_ver: int, allowlist_sha: str) -> dict:
    try:
        raw, _ = SIO.read_nofollow(run_dir, RECORD_REL)
        doc = json.loads(raw.decode("utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("fetches"), dict):
            return doc
    except FileNotFoundError:
        pass
    except (ValueError, UnicodeDecodeError) as e:
        raise ArtifactTampered("acquire_record_invalid", f"{RECORD_REL}: {e}") from None
    return {
        "allowlist_version": allowlist_ver,
        "allowlist_sha256": allowlist_sha,
        "fetches": {},
        "catalogue_codes": {},
        "intents": {},
    }


def _save_record(run_dir: str, rec: dict) -> None:
    SIO.write_nofollow(run_dir, RECORD_REL, json.dumps(rec, indent=1, sort_keys=True).encode("utf-8"), atomic=True)


def _load_provenance(run_dir: str) -> dict:
    try:
        raw, _ = SIO.read_nofollow(run_dir, PROVENANCE_REL)
        doc = json.loads(raw.decode("utf-8"))
        if isinstance(doc, dict):
            doc.setdefault("inputs", {})
            doc.setdefault("cited", {})
            return doc
    except FileNotFoundError:
        pass
    except (ValueError, UnicodeDecodeError) as e:
        raise ArtifactTampered("provenance_invalid", f"{PROVENANCE_REL}: {e}") from None
    return {"inputs": {}, "cited": {}}


def _save_provenance(run_dir: str, prov: dict) -> None:
    SIO.write_nofollow(run_dir, PROVENANCE_REL, json.dumps(prov, indent=1, sort_keys=True).encode("utf-8"), atomic=True)


def _compute_inventory_sha(acquired_entries: Mapping[str, Mapping], manifest_bytes: bytes) -> str:
    pairs = sorted((str(p), str(e["sha256"])) for p, e in acquired_entries.items())
    h = hashlib.sha256()
    h.update(json.dumps(pairs, separators=(",", ":")).encode("utf-8"))
    h.update(b"\n")
    h.update(manifest_bytes)
    return h.hexdigest()


# --------------------------------------------------------------------------- Fetcher (§3.2)
class Fetcher:
    """Harness HTTP(S) fetcher: allowlist-enforced, SSRF-pinned, digest-keyed, quota-bounded."""

    def __init__(
        self,
        run_dir: str,
        allowlist_path: str = DEFAULT_ALLOWLIST,
        *,
        resolver: Callable[[str, int], list[str]] | None = None,
        ssl_context: ssl.SSLContext | None = None,
        max_file_bytes: int = MAX_FILE_BYTES,
        max_run_bytes: int = MAX_RUN_BYTES,
        max_fetches_round: int = MAX_FETCHES_PER_ROUND,
        max_fetches_run: int = MAX_FETCHES_PER_RUN,
        _allow_test_http: bool = False,
        _test_port: int | None = None,
        _allow_loopback_ip: bool = False,
        _user_agent: str = "modeling-agentic-harness/v2",
    ):
        self.run_dir = os.path.abspath(run_dir)
        self.allowlist, self.allowlist_sha = load_allowlist(allowlist_path)
        self.resolver = resolver or _default_resolver
        self.ssl_context = ssl_context or ssl.create_default_context()
        self.max_file_bytes = int(max_file_bytes)
        self.max_run_bytes = int(max_run_bytes)
        self.max_fetches_round = int(max_fetches_round)
        self.max_fetches_run = int(max_fetches_run)
        self._allow_test_http = bool(_allow_test_http)
        self._test_port = _test_port
        self._allow_loopback_ip = bool(_allow_loopback_ip)
        self._user_agent = _user_agent
        self._last_req_ts: dict[str, float] = {}
        self._round_fetches = 0
        self.record = _load_record(self.run_dir, int(self.allowlist["version"]), self.allowlist_sha)
        self._resume_cleanup()

    def _resume_cleanup(self) -> None:
        """Delete stale `.part` downloads and hash-mismatched quarantine files via safe_io (AQ14, AQ17)."""
        # Verify acquire/quarantine is never a symlink (AQ17)
        try:
            entries = SIO.listdir_nofollow(self.run_dir, QUARANTINE_DIR)
        except FileNotFoundError:
            return
        changed = False
        for fn in entries:
            rel = f"{QUARANTINE_DIR}/{fn}"
            if fn.endswith(".part"):
                SIO.remove_nofollow(self.run_dir, rel)
                changed = True
                continue
            fid = fn.split(".")[0]
            rec = self.record["fetches"].get(fid)
            if rec is None or rec.get("status") != "ok":
                SIO.remove_nofollow(self.run_dir, rel)
                changed = True
                continue
            raw, _ = SIO.read_nofollow(self.run_dir, rel)
            if hashlib.sha256(raw).hexdigest() != rec.get("sha256"):
                SIO.remove_nofollow(self.run_dir, rel)
                rec["status"] = "discarded_on_resume"
                changed = True
        if changed:
            _save_record(self.run_dir, self.record)

    def list_sources(self) -> list[dict]:
        """Return enabled allowlist entries (id, holds, endpoints, wanted_kinds, licence summary)."""
        out = []
        for s in self.allowlist["sources"]:
            if s["status"] != "enabled" or s["requires_key"]:
                continue
            out.append({
                "id": s["id"],
                "holds": s["holds"],
                "licence": {k: s["licence"][k] for k in ("id", "terms_url", "redistribution")},
                "endpoints": {
                    en: {"wanted_kinds": list(ep["wanted_kinds"]), "params": dict(ep["params"])}
                    for en, ep in s["endpoints"].items()
                },
            })
        return out

    def _validate_params(self, src_id: str, ep_name: str, ep: Mapping, params: Mapping) -> dict[str, str]:
        schema = ep["params"]
        clean: dict[str, str] = {}
        for k in params:
            if k not in schema:
                raise FetchRefused("fetch_param_refused", f"{src_id}.{ep_name}: unknown parameter {k!r}")
        for pname, pspec in schema.items():
            if pname not in params or params[pname] is None:
                if pspec.get("required"):
                    raise FetchRefused("fetch_param_refused", f"{src_id}.{ep_name}: missing required parameter {pname!r}")
                continue
            raw_v = params[pname]
            val = str(raw_v).strip()
            if _URL_LIKE_RE.search(val):
                raise FetchRefused("fetch_param_refused", f"{src_id}.{ep_name}.{pname}: URL/path characters forbidden")
            ptype = pspec["type"]
            if ptype == "enum":
                if val not in [str(x) for x in pspec["values"]]:
                    raise FetchRefused("fetch_param_refused", f"{src_id}.{ep_name}.{pname}: {val!r} not in {pspec['values']}")
            elif ptype == "int_range":
                if isinstance(raw_v, bool) or not re.fullmatch(r"-?[0-9]+", val):
                    raise FetchRefused("fetch_param_refused", f"{src_id}.{ep_name}.{pname}: integer required")
                iv = int(val)
                if not (int(pspec["min"]) <= iv <= int(pspec["max"])):
                    raise FetchRefused("fetch_param_refused", f"{src_id}.{ep_name}.{pname}: {iv} outside [{pspec['min']}, {pspec['max']}]")
                val = str(iv)
            elif ptype == "terms":
                if not val or len(val) > MAX_TERMS_CHARS or not _TERMS_RE.fullmatch(val):
                    raise FetchRefused("fetch_param_refused", f"{src_id}.{ep_name}.{pname}: terms must be alphanumeric/spaces <= {MAX_TERMS_CHARS} chars")
                if len(val.split()) > MAX_TERMS_WORDS:
                    raise FetchRefused("fetch_param_refused", f"{src_id}.{ep_name}.{pname}: at most {MAX_TERMS_WORDS} words allowed")
            elif ptype in ("code", "pattern"):
                if not re.fullmatch(pspec["pattern"], val):
                    raise FetchRefused("fetch_param_refused", f"{src_id}.{ep_name}.{pname}: {val!r} does not match pattern")
                if ptype == "code" and pspec.get("from_catalogue"):
                    cat_key = str(pspec["from_catalogue"])
                    known = set(self.record.get("catalogue_codes", {}).get(cat_key) or [])
                    parts = [x.strip() for x in re.split(r"[,;]", val) if x.strip()]
                    if not known or any(p not in known for p in parts):
                        raise FetchRefused("fetch_param_refused", f"{src_id}.{ep_name}.{pname}: code {val!r} not found in catalogue {cat_key}")
            clean[pname] = val
        return clean

    def _build_url(self, ep: Mapping, clean_params: Mapping[str, str]) -> tuple[str, str, int, str]:
        scheme = "http" if self._allow_test_http else "https"
        port = int(self._test_port) if (self._allow_test_http and self._test_port) else (80 if self._allow_test_http else 443)
        host = str(ep["host"])
        path = str(ep["path_template"])
        used_in_path: set[str] = set()
        for ph in _PLACEHOLDER_RE.findall(path):
            used_in_path.add(ph)
            path = path.replace("{" + ph + "}", urllib.parse.quote(clean_params[ph], safe=""))
        q_items = sorted((k, v) for k, v in clean_params.items() if k not in used_in_path)
        query = urllib.parse.urlencode(q_items)
        if len(query) > MAX_QUERY_CHARS:
            raise FetchRefused("fetch_param_refused", f"query string exceeds {MAX_QUERY_CHARS} chars")
        target = f"{path}?{query}" if query else path
        port_suf = f":{port}" if (self._allow_test_http and port != 80) else ""
        full_url = f"{scheme}://{host}{port_suf}{target}"
        return scheme, host, port, full_url

    def _resolve_pinned(self, host: str, port: int) -> str:
        try:
            addrs = self.resolver(host, port)
        except OSError as e:
            raise FetchRefused("source_unavailable", f"DNS resolution failed for {host}: {e}") from None
        if not addrs:
            raise FetchRefused("source_unavailable", f"DNS returned no addresses for {host}")
        for ip in addrs:
            if self._allow_loopback_ip and ip in ("127.0.0.1", "::1"):
                continue
            if not _is_public_ip(ip):
                raise FetchRefused("fetch_refused", f"resolved non-public address {ip} for {host}")
        return addrs[0]

    def _check_hop(self, url: str, ep: Mapping, path_re: re.Pattern) -> tuple[str, str, int, str]:
        u = urllib.parse.urlsplit(url)
        exp_scheme = "http" if self._allow_test_http else "https"
        exp_port = int(self._test_port) if (self._allow_test_http and self._test_port) else (80 if self._allow_test_http else 443)
        if u.scheme.lower() != exp_scheme:
            raise FetchRefused("fetch_refused", f"disallowed scheme {u.scheme!r}")
        if (u.hostname or "").lower() != str(ep["host"]).lower():
            raise FetchRefused("fetch_refused", f"host {u.hostname!r} outside allowlist host {ep['host']!r}")
        actual_port = u.port or (80 if u.scheme.lower() == "http" else 443)
        if actual_port != exp_port:
            raise FetchRefused("fetch_refused", f"port {actual_port} != allowed port {exp_port}")
        if not path_re.match(u.path or "/"):
            raise FetchRefused("fetch_refused", f"path {u.path!r} outside endpoint template {ep['path_template']!r}")
        target = f"{u.path}?{u.query}" if u.query else (u.path or "/")
        return exp_scheme, str(ep["host"]), exp_port, target

    def _total_recorded_fetches_and_bytes(self) -> tuple[int, int]:
        n = len(self.record["fetches"])
        b = sum(int(r.get("bytes") or 0) for r in self.record["fetches"].values() if r.get("status") == "ok")
        return n, b

    def fetch(
        self,
        source_id: str,
        endpoint: str,
        params: Mapping | None = None,
        *,
        wanted: list[str] | None = None,
        tool: str = "fetch",
        found_by: str | None = None,
    ) -> dict:
        """Fetch an allowlisted endpoint into quarantine, returning fetch id, metadata, and sanitized preview."""
        src = self.allowlist["_by_id"].get(source_id)
        if src is None:
            raise FetchRefused("fetch_refused", f"unknown source {source_id!r}")
        if src["status"] != "enabled":
            raise FetchRefused("fetch_refused", f"source {source_id!r} is excluded ({src.get('reason')})")
        if src["requires_key"]:
            raise FetchRefused("fetch_refused", f"source {source_id!r} requires a key (refused in v1)")
        ep = src["endpoints"].get(endpoint)
        if ep is None:
            raise FetchRefused("fetch_refused", f"unknown endpoint {source_id}.{endpoint}")
        clean_params = self._validate_params(source_id, endpoint, ep, params or {})
        wanted_list = [str(w) for w in (wanted or ep["wanted_kinds"]) if str(w) in self.allowlist["wanted_kinds"]]
        digest = _canonical_digest(source_id, endpoint, clean_params, self.allowlist_sha)

        # Idempotent resume: return stored result without re-fetching (AQ14)
        prev = self.record["fetches"].get(digest)
        if prev and prev.get("status") == "ok":
            q_rel = prev.get("quarantine_rel")
            p_rel = prev.get("promoted_rel")
            for cand in (q_rel, p_rel):
                if cand:
                    try:
                        raw_old, _ = SIO.read_nofollow(self.run_dir, cand)
                        if hashlib.sha256(raw_old).hexdigest() == prev["sha256"]:
                            return {
                                "fetch_id": digest,
                                "cached": True,
                                "status": "ok",
                                "bytes": prev["bytes"],
                                "sha256": prev["sha256"],
                                "content_type": prev["content_type"],
                                "preview": f"<fetched source={source_id} id={digest}>\n{sanitize_preview(raw_old)}\n</fetched>",
                            }
                    except FileNotFoundError:
                        continue

        # Quota checks from persistent record (AQ14)
        total_n, total_b = self._total_recorded_fetches_and_bytes()
        if self._round_fetches >= self.max_fetches_round or total_n >= self.max_fetches_run:
            raise FetchRefused("fetch_quota_exceeded", f"fetch limit reached (round={self._round_fetches}, run={total_n})")
        if total_b >= self.max_run_bytes:
            raise FetchRefused("acquire_oversized", f"run byte cap reached ({total_b} >= {self.max_run_bytes})")

        scheme, host, port, full_url = self._build_url(ep, clean_params)
        path_re = _template_path_regex(ep["path_template"], ep["params"])

        # Per-source rate limit
        min_gap = 1.0 / max(float(src["rate_limit"]["rps"]), 0.01)
        last_ts = self._last_req_ts.get(source_id)
        if last_ts is not None and (time.monotonic() - last_ts) < min_gap:
            time.sleep(min_gap - (time.monotonic() - last_ts))

        # Verify quarantine directory is not a symlink BEFORE creating anything (AQ17)
        dfd_q = SIO.dir_fd_nofollow(self.run_dir, QUARANTINE_DIR.split("/"), create=True)
        os.close(dfd_q)

        # Record intent (`started`) before network call (AQ14)
        self.record["fetches"][digest] = {
            "fetch_id": digest,
            "source_id": source_id,
            "endpoint": endpoint,
            "params": dict(clean_params),
            "wanted": wanted_list,
            "tool": tool,
            "found_by": found_by,
            "url": full_url,
            "status": "started",
            "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        }
        _save_record(self.run_dir, self.record)
        self._round_fetches += 1

        cur_url = full_url
        deadline = time.monotonic() + TOTAL_TIMEOUT_S
        part_rel = f"{QUARANTINE_DIR}/{digest}.part"
        q_rel = f"{QUARANTINE_DIR}/{digest}.bin"

        try:
            resp = None
            conn = None
            for hop in range(MAX_REDIRECTS + 1):
                h_scheme, h_host, h_port, h_target = self._check_hop(cur_url, ep, path_re)
                pinned_ip = self._resolve_pinned(h_host, h_port)
                rem_t = max(1.0, min(CONNECT_TIMEOUT_S, deadline - time.monotonic()))
                if h_scheme == "https":
                    conn = _PinnedHTTPSConnection(h_host, h_port, pinned_ip=pinned_ip, timeout=rem_t,
                                                  context=self.ssl_context)
                else:
                    conn = _PinnedHTTPConnection(h_host, h_port, pinned_ip=pinned_ip, timeout=rem_t)
                self._last_req_ts[source_id] = time.monotonic()
                conn.request("GET", h_target, headers={
                    "User-Agent": self._user_agent,
                    "Accept": ", ".join(ep["allowed_content_types"]),
                    "Connection": "close",
                })
                resp = conn.getresponse()
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.getheader("Location")
                    resp.read(0)
                    conn.close()
                    conn = None
                    if not loc or hop >= MAX_REDIRECTS:
                        raise FetchRefused("fetch_refused", f"redirect refused (hop={hop}, location={loc!r})")
                    cur_url = urllib.parse.urljoin(cur_url, loc)
                    continue
                break

            assert resp is not None and conn is not None
            try:
                if resp.status != 200:
                    raise FetchRefused("source_unavailable", f"HTTP {resp.status} from {source_id}.{endpoint}")
                ct_hdr = resp.getheader("Content-Type") or ""
                etag = resp.getheader("ETag")
                last_mod = resp.getheader("Last-Modified")
                h = hashlib.sha256()
                total = 0
                preview_buf = bytearray()
                # Stream via O_NOFOLLOW | O_EXCL temporary file (AQ6, AQ17): aborts and unlinks temp on error
                with SIO.stream_nofollow(self.run_dir, q_rel) as sf:
                    while True:
                        if time.monotonic() > deadline:
                            raise FetchRefused("source_unavailable", "total transfer timeout exceeded")
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > self.max_file_bytes or (total_b + total) > self.max_run_bytes:
                            raise FetchRefused("oversized", f"response exceeded cap ({total} bytes)")
                        h.update(chunk)
                        if len(preview_buf) < MAX_PREVIEW_BYTES:
                            preview_buf.extend(chunk[:MAX_PREVIEW_BYTES - len(preview_buf)])
                        sf.write(chunk)
            finally:
                conn.close()
        except FetchRefused as e:
            SIO.remove_nofollow(self.run_dir, part_rel, missing_ok=True)
            self.record["fetches"][digest].update(status=e.code, error=str(e))
            _save_record(self.run_dir, self.record)
            raise
        except OSError as e:
            SIO.remove_nofollow(self.run_dir, part_rel, missing_ok=True)
            self.record["fetches"][digest].update(status="source_unavailable", error=f"{type(e).__name__}: {e}")
            _save_record(self.run_dir, self.record)
            raise FetchRefused("source_unavailable", f"{type(e).__name__}: {e}") from None

        sha_hex = h.hexdigest()
        rec_entry = self.record["fetches"][digest]
        rec_entry.update(
            status="ok",
            url=cur_url,
            retrieved_at=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            sha256=sha_hex,
            bytes=total,
            content_type=ct_hdr,
            http={"status": 200, "etag": etag, "last_modified": last_mod},
            quarantine_rel=q_rel,
        )
        # Extract catalogue codes for downstream AQ2b binding if endpoint declares `catalogue_field`
        cat_field = ep.get("catalogue_field")
        if cat_field:
            raw_q, _ = SIO.read_nofollow(self.run_dir, q_rel)
            codes = _extract_catalogue_codes(raw_q, ct_hdr, str(cat_field))
            if codes:
                cat_key = f"{source_id}:{endpoint}"
                existing = set(self.record.setdefault("catalogue_codes", {}).get(cat_key) or [])
                self.record["catalogue_codes"][cat_key] = sorted(existing | codes)
        _save_record(self.run_dir, self.record)
        return {
            "fetch_id": digest,
            "cached": False,
            "status": "ok",
            "bytes": total,
            "sha256": sha_hex,
            "content_type": ct_hdr,
            "preview": f"<fetched source={source_id} id={digest}>\n{sanitize_preview(bytes(preview_buf))}\n</fetched>",
        }

    def search(self, source_id: str, endpoint: str, params: Mapping | None = None, *, wanted: list[str] | None = None) -> dict:
        """Execute a source's search/catalogue endpoint and record it like any fetch."""
        return self.fetch(source_id, endpoint, params, wanted=wanted, tool="search")


# --------------------------------------------------------------------------- Promotion (§4, AQ7-AQ11, AQ15/15b)
def _recover_intents(run_dir: str, rec: dict, prov: dict, allowlist: Mapping) -> None:
    """Complete or fail-closed any unfinished promotion intents after a crash (AQ15b)."""
    intents = dict(rec.get("intents") or {})
    if not intents:
        return
    for fid, intent in list(intents.items()):
        dest_rel = str(intent["dest_rel"])
        exp_sha = str(intent["sha256"])
        try:
            dest_bytes, _ = SIO.read_nofollow(run_dir, dest_rel)
        except FileNotFoundError:
            dest_bytes = None
        if dest_bytes is not None:
            got_sha = hashlib.sha256(dest_bytes).hexdigest()
            if got_sha != exp_sha:
                raise ArtifactTampered("artifact_tampered", f"recovered destination {dest_rel} hash mismatch")
            SIO.chmod_nofollow(run_dir, dest_rel, 0o444)
        else:
            q_rel = intent.get("quarantine_rel")
            if not q_rel:
                raise ArtifactTampered("artifact_tampered", f"intent {fid} has neither destination nor quarantine")
            q_bytes, _ = SIO.read_nofollow(run_dir, q_rel)
            if hashlib.sha256(q_bytes).hexdigest() != exp_sha:
                raise ArtifactTampered("artifact_tampered", f"quarantine {q_rel} hash mismatch during intent recovery")
            SIO.write_nofollow(run_dir, dest_rel, q_bytes, atomic=True)
            SIO.chmod_nofollow(run_dir, dest_rel, 0o444)
            dest_bytes = q_bytes
        if dest_rel not in prov["inputs"]:
            prov["inputs"][dest_rel] = dict(intent["provenance_entry"])
        view = build_schema_view(dest_bytes, intent["content_type"], fid, intent["source_id"], intent["endpoint"])
        SIO.write_nofollow(run_dir, f"{VIEWS_ROOT}/{fid}.schema.json",
                           json.dumps(view, indent=1, sort_keys=True).encode("utf-8"), atomic=True)
        if intent.get("quarantine_rel"):
            SIO.remove_nofollow(run_dir, intent["quarantine_rel"], missing_ok=True)
        frec = rec["fetches"].get(fid)
        if frec is not None:
            frec["promoted_rel"] = dest_rel
            frec["quarantine_rel"] = None
        rec["intents"].pop(fid, None)
    _save_provenance(run_dir, prov)
    _save_record(run_dir, rec)


def _write_manifest_and_digest(run_dir: str, prov: dict, rec: dict, rejected: list[dict]) -> str:
    acq_inputs = {p: e for p, e in (prov.get("inputs") or {}).items()
                  if isinstance(e, Mapping) and e.get("origin") == "acquired"}
    manifest = {
        "allowlist_version": rec.get("allowlist_version"),
        "allowlist_sha256": rec.get("allowlist_sha256"),
        "files": {
            p: {
                "fetch_id": e["request_digest"],
                "sha256": e["sha256"],
                "bytes": e["bytes"],
                "content_type": e["content_type"],
                "source_id": e["source_id"],
                "endpoint": e["endpoint"],
                "url": e["url"],
                "retrieved_at": e["retrieved_at"],
                "licence": e["licence"],
                "wanted": e["wanted"],
                "view": f"{VIEWS_ROOT}/{e['request_digest']}.schema.json",
            }
            for p, e in sorted(acq_inputs.items())
        },
        "queries": [
            {
                "fetch_id": fid,
                "source_id": r["source_id"],
                "endpoint": r["endpoint"],
                "params": r.get("params") or {},
                "status": r.get("status"),
            }
            for fid, r in sorted((rec.get("fetches") or {}).items())
        ],
        "rejected": list(rejected),
    }
    manifest_bytes = json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8")
    inv_sha = _compute_inventory_sha(acq_inputs, manifest_bytes)
    prov["acquired_inventory_sha256"] = inv_sha
    SIO.write_nofollow(run_dir, MANIFEST_REL, manifest_bytes, atomic=True)
    _save_provenance(run_dir, prov)
    return inv_sha


def promote(
    run_dir: str,
    keep_ids: list[str],
    wanted_map: Mapping[str, list[str]] | None = None,
    *,
    allowlist_path: str = DEFAULT_ALLOWLIST,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_run_bytes: int = MAX_RUN_BYTES,
    _stop_after_copy: bool = False,
) -> dict:
    """Nine-step harness promotion from `acquire/quarantine/` to `data/inputs/acquired/` (§4, AQ7-AQ11, AQ15/15b)."""
    run_dir = os.path.abspath(run_dir)
    allowlist, allowlist_sha = load_allowlist(allowlist_path)
    rec = _load_record(run_dir, int(allowlist["version"]), allowlist_sha)
    prov = _load_provenance(run_dir)
    _recover_intents(run_dir, rec, prov, allowlist)

    promoted: list[str] = []
    rejected: list[dict] = []
    total_promoted_bytes = sum(
        int(e.get("bytes") or 0) for e in (prov.get("inputs") or {}).values()
        if isinstance(e, Mapping) and e.get("origin") == "acquired"
    )
    keep_unique = list(dict.fromkeys(str(fid) for fid in keep_ids))

    for fid in keep_unique:
        frec = rec["fetches"].get(fid)
        # Step 1: recorded fetch id from this run & allowlist version
        if frec is None or frec.get("status") != "ok":
            rejected.append({"fetch_id": fid, "code": "acquire_unrecorded", "reason": "fetch id not recorded ok"})
            continue
        # Idempotent if already promoted (AQ15)
        if frec.get("promoted_rel") and frec["promoted_rel"] in prov["inputs"]:
            promoted.append(frec["promoted_rel"])
            continue
        src_id, ep_name = str(frec["source_id"]), str(frec["endpoint"])
        src = allowlist["_by_id"].get(src_id)
        ep = src["endpoints"].get(ep_name) if src and src["status"] == "enabled" and not src["requires_key"] else None
        if src is None or ep is None:
            rejected.append({"fetch_id": fid, "code": "acquire_unrecorded", "reason": "source/endpoint not enabled"})
            continue
        q_rel = frec.get("quarantine_rel")
        if not q_rel:
            rejected.append({"fetch_id": fid, "code": "acquire_unrecorded", "reason": "quarantined file missing"})
            continue

        # Step 6 (read & hash via safe_io; AQ9, AQ17)
        raw, _ = SIO.read_nofollow(run_dir, q_rel)
        actual_sha = hashlib.sha256(raw).hexdigest()
        if actual_sha != frec.get("sha256"):
            raise ArtifactTampered("artifact_tampered", f"quarantined {fid} hash mismatch ({actual_sha} != {frec.get('sha256')})")

        # Step 2: size bounds
        if len(raw) == 0 or len(raw) > max_file_bytes or (total_promoted_bytes + len(raw)) > max_run_bytes:
            SIO.remove_nofollow(run_dir, q_rel, missing_ok=True)
            frec["quarantine_rel"] = None
            rejected.append({"fetch_id": fid, "code": "acquire_oversized", "reason": f"size {len(raw)} out of bounds"})
            continue

        # Step 3: content-type header + byte sniff agreement (AQ7)
        ct_clean = sniff_content_type(raw, str(frec.get("content_type") or ""))
        if ct_clean is None or ct_clean not in set(ep["allowed_content_types"]):
            SIO.remove_nofollow(run_dir, q_rel, missing_ok=True)
            frec["quarantine_rel"] = None
            rejected.append({"fetch_id": fid, "code": "acquire_type_mismatch",
                             "reason": f"header/sniff mismatch or disallowed type ({frec.get('content_type')!r})"})
            continue

        # Step 4: strict parse (AQ8)
        ok_parse, parsed, perr = validate_and_parse(raw, ct_clean)
        if not ok_parse:
            SIO.remove_nofollow(run_dir, q_rel, missing_ok=True)
            frec["quarantine_rel"] = None
            rejected.append({"fetch_id": fid, "code": "acquire_unparseable", "reason": perr})
            continue

        # Step 5: licence + per-record licence check (AQ11)
        ok_lic, rec_lics, lerr = check_licence(src, allowlist, parsed, ct_clean)
        if not ok_lic:
            SIO.remove_nofollow(run_dir, q_rel, missing_ok=True)
            frec["quarantine_rel"] = None
            rejected.append({"fetch_id": fid, "code": "acquire_licence_unclear", "reason": lerr})
            continue

        # Step 7: promotion intent, atomic no-follow copy, re-hash, chmod 0444 (AQ10, AQ15b)
        ext = EXT_FOR_TYPE[ct_clean]
        dest_rel = f"{ACQUIRED_ROOT}/{src_id}/{fid}.{ext}"
        wanted_final = list((wanted_map or {}).get(fid) or frec.get("wanted") or ep["wanted_kinds"])
        prov_entry = {
            "origin": "acquired",
            "sha256": actual_sha,
            "bytes": len(raw),
            "content_type": ct_clean,
            "url": frec["url"],
            "retrieved_at": frec["retrieved_at"],
            "http": dict(frec.get("http") or {}),
            "source_id": src_id,
            "endpoint": ep_name,
            "allowlist_version": rec["allowlist_version"],
            "allowlist_sha256": rec["allowlist_sha256"],
            "licence": {
                "id": src["licence"]["id"],
                "terms_url": src["licence"]["terms_url"],
                "redistribution": src["licence"]["redistribution"],
                **({"record_licences": rec_lics} if rec_lics else {}),
            },
            "query": {
                "tool": frec.get("tool", "fetch"),
                "params": dict(frec.get("params") or {}),
                "found_by": frec.get("found_by"),
            },
            "wanted": wanted_final,
            "request_digest": fid,
        }
        rec.setdefault("intents", {})[fid] = {
            "dest_rel": dest_rel,
            "sha256": actual_sha,
            "quarantine_rel": q_rel,
            "content_type": ct_clean,
            "source_id": src_id,
            "endpoint": ep_name,
            "provenance_entry": prov_entry,
        }
        _save_record(run_dir, rec)

        SIO.write_nofollow(run_dir, dest_rel, raw, atomic=True)
        dest_raw, _ = SIO.read_nofollow(run_dir, dest_rel)
        if hashlib.sha256(dest_raw).hexdigest() != actual_sha:
            raise ArtifactTampered("artifact_tampered", f"promoted destination {dest_rel} hash mismatch after copy")
        SIO.chmod_nofollow(run_dir, dest_rel, 0o444)

        if _stop_after_copy:
            return {"interrupted": True, "promoted": promoted, "rejected": rejected}

        # Step 8: provenance, sanitized view, close intent, remove quarantined file
        prov["inputs"][dest_rel] = prov_entry
        view = build_schema_view(raw, ct_clean, fid, src_id, ep_name)
        SIO.write_nofollow(run_dir, f"{VIEWS_ROOT}/{fid}.schema.json",
                           json.dumps(view, indent=1, sort_keys=True).encode("utf-8"), atomic=True)
        SIO.remove_nofollow(run_dir, q_rel, missing_ok=True)
        frec["promoted_rel"] = dest_rel
        frec["quarantine_rel"] = None
        rec["intents"].pop(fid, None)
        _save_provenance(run_dir, prov)
        _save_record(run_dir, rec)
        total_promoted_bytes += len(raw)
        promoted.append(dest_rel)

    # Delete any remaining unkept quarantined files
    for fid, frec in rec["fetches"].items():
        if fid not in keep_unique and frec.get("quarantine_rel"):
            SIO.remove_nofollow(run_dir, frec["quarantine_rel"], missing_ok=True)
            frec["quarantine_rel"] = None
            rejected.append({"fetch_id": fid, "code": "not_kept", "reason": "not listed in keep"})
    _save_record(run_dir, rec)

    # Step 9: inventory digest & manifest
    inv_sha = _write_manifest_and_digest(run_dir, prov, rec, rejected)
    return {"promoted": promoted, "rejected": rejected, "inventory_sha256": inv_sha}


# --------------------------------------------------------------------------- Inventory verification (§4, AQ13, AQ18)
def verify_inventory(run_dir: str) -> dict:
    """Verify every file under `data/inputs/acquired/` against provenance by sha256 (AQ13, AQ18).
    Restores `0444` on verified files whose mode changed on checkpoint restore; fails closed on any
    missing, extra, changed, or symlinked entry."""
    run_dir = os.path.abspath(run_dir)
    prov = _load_provenance(run_dir)
    acq_prov = {p: e for p, e in (prov.get("inputs") or {}).items()
                if isinstance(e, Mapping) and e.get("origin") == "acquired"}

    # Walk data/inputs/acquired/ without following any links (excluding _views/)
    found_files: set[str] = set()
    try:
        top_entries = SIO.listdir_nofollow(run_dir, ACQUIRED_ROOT)
    except FileNotFoundError:
        top_entries = []
    except SIO.LinkRefused as e:
        raise ArtifactTampered("artifact_tampered", f"symlink in {ACQUIRED_ROOT}: {e}") from None

    for sub in top_entries:
        if sub == "_views":
            continue
        sub_rel = f"{ACQUIRED_ROOT}/{sub}"
        # Every immediate entry under data/inputs/acquired/ (other than _views) must be a plain source dir
        try:
            children = SIO.listdir_nofollow(run_dir, sub_rel)
        except SIO.LinkRefused as e:
            raise ArtifactTampered("artifact_tampered", f"non-directory or symlink at {sub_rel}: {e}") from None
        for fn in children:
            found_files.add(f"{sub_rel}/{fn}")

    expected_files = set(acq_prov.keys())
    missing = sorted(expected_files - found_files)
    extra = sorted(found_files - expected_files)
    if missing:
        raise ArtifactTampered("artifact_tampered", f"missing acquired file(s): {missing[:3]}")
    if extra:
        raise ArtifactTampered("artifact_tampered", f"extra untracked acquired file(s): {extra[:3]}")

    restored_modes: list[str] = []
    for rel in sorted(expected_files):
        entry = acq_prov[rel]
        try:
            raw, st = SIO.read_nofollow(run_dir, rel)
        except (FileNotFoundError, SIO.LinkRefused, OSError) as e:
            raise ArtifactTampered("artifact_tampered", f"cannot verify {rel}: {e}") from None
        if st.st_nlink > 1:
            raise ArtifactTampered("artifact_tampered", f"{rel}: multiple hard links refused")
        if hashlib.sha256(raw).hexdigest() != entry.get("sha256"):
            raise ArtifactTampered("artifact_tampered", f"{rel}: sha256 mismatch")
        if (st.st_mode & 0o777) != 0o444:
            SIO.chmod_nofollow(run_dir, rel, 0o444)
            restored_modes.append(rel)
        # Rebuild & compare sanitized view
        fid = str(entry["request_digest"])
        exp_view = build_schema_view(raw, entry["content_type"], fid, entry["source_id"], entry["endpoint"])
        view_rel = f"{VIEWS_ROOT}/{fid}.schema.json"
        try:
            vraw, _ = SIO.read_nofollow(run_dir, view_rel)
            if json.loads(vraw.decode("utf-8")) != exp_view:
                raise ArtifactTampered("artifact_tampered", f"view mismatch for {view_rel}")
        except (FileNotFoundError, SIO.LinkRefused, ValueError) as e:
            raise ArtifactTampered("artifact_tampered", f"view invalid for {view_rel}: {e}") from None

    if not expected_files and "acquired_inventory_sha256" not in prov:
        return {"ok": True, "files": 0, "inventory_sha256": None, "restored_modes": []}

    try:
        manifest_bytes, _ = SIO.read_nofollow(run_dir, MANIFEST_REL)
    except (FileNotFoundError, SIO.LinkRefused) as e:
        raise ArtifactTampered("artifact_tampered", f"manifest missing or symlinked: {e}") from None
    inv_sha = _compute_inventory_sha(acq_prov, manifest_bytes)
    if prov.get("acquired_inventory_sha256") != inv_sha:
        raise ArtifactTampered("artifact_tampered", f"acquired_inventory_sha256 mismatch ({inv_sha} != {prov.get('acquired_inventory_sha256')})")
    return {"ok": True, "files": len(expected_files), "inventory_sha256": inv_sha, "restored_modes": restored_modes}


# --------------------------------------------------------------------------- Local fixture server & self-test (AQ1-AQ18)
class AcquireFixtureServer:
    """In-process synthetic HTTP fixture server for AQ2-AQ18 self-tests (zero external network)."""

    def __init__(self, routes: dict[str, tuple[int, str, bytes, dict[str, str]]]):
        self.routes = routes
        self.requests_seen: list[dict] = []
        server_ref = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                server_ref.requests_seen.append({
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                })
                row = server_ref.routes.get(self.path)
                if row is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                status, ctype, body, extra = row
                self.send_response(status)
                if ctype:
                    self.send_header("Content-Type", ctype)
                for hk, hv in (extra or {}).items():
                    self.send_header(hk, hv)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def log_message(self, *_args):
                return

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def __enter__(self) -> AcquireFixtureServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._srv.shutdown()
        self._srv.server_close()


def _test_allowlist_yaml(port: int) -> dict:
    return {
        "version": 1,
        "wanted_kinds": ["population", "burden", "trial_effect"],
        "accepted_licences": ["CC-BY-4.0", "EUROPEPMC-METADATA-OPEN"],
        "sources": [
            {
                "id": "svc_data",
                "holds": "synthetic tabular indicator service",
                "status": "enabled",
                "requires_key": False,
                "rate_limit": {"rps": 100.0},
                "licence": {"id": "CC-BY-4.0", "terms_url": "https://example.org/terms",
                            "checked": "2026-11-26", "redistribution": "allowed"},
                "endpoints": {
                    "catalogue": {
                        "host": "fixture.local",
                        "path_template": "/api/catalogue",
                        "allowed_content_types": ["application/json"],
                        "wanted_kinds": ["population"],
                        "catalogue_field": "code",
                        "params": {},
                    },
                    "slice": {
                        "host": "fixture.local",
                        "path_template": "/api/slice/{code}",
                        "allowed_content_types": ["application/json", "text/csv", "application/xml"],
                        "wanted_kinds": ["population", "burden"],
                        "params": {
                            "code": {"type": "code", "pattern": "^[A-Z0-9_]+$",
                                     "from_catalogue": "svc_data:catalogue", "required": True},
                            "mode": {"type": "enum", "values": ["ok", "csv", "bad_csv", "bad_json", "html",
                                                                "big", "redir_host", "redir_ep", "xxe", "xml_ok"],
                                     "required": False},
                        },
                    },
                },
            },
            {
                "id": "svc_lit",
                "holds": "synthetic per-record literature service",
                "status": "enabled",
                "requires_key": False,
                "rate_limit": {"rps": 100.0},
                "licence": {"id": "EUROPEPMC-METADATA-OPEN", "terms_url": "https://example.org/lit",
                            "checked": "2026-11-26", "redistribution": "per_record",
                            "accepted_record_licences": ["CC-BY-4.0", "CC0-1.0"]},
                "endpoints": {
                    "search": {
                        "host": "fixture.local",
                        "path_template": "/lit/search",
                        "allowed_content_types": ["application/json"],
                        "wanted_kinds": ["trial_effect"],
                        "params": {
                            "q": {"type": "terms", "required": True},
                        },
                    },
                },
            },
            {
                "id": "svc_key",
                "holds": "keyed service",
                "status": "enabled",
                "requires_key": True,
                "rate_limit": {"rps": 10.0},
                "licence": {"id": "CC-BY-4.0", "terms_url": "https://example.org/key",
                            "checked": "2026-11-26", "redistribution": "allowed"},
                "endpoints": {
                    "get": {"host": "fixture.local", "path_template": "/k", "allowed_content_types": ["application/json"],
                            "wanted_kinds": ["burden"], "params": {}},
                },
            },
            {
                "id": "svc_off",
                "holds": "excluded service",
                "status": "excluded",
                "reason": "excluded for test",
                "requires_key": False,
                "rate_limit": {"rps": 10.0},
                "licence": {"id": "CC-BY-4.0", "terms_url": "https://example.org/off",
                            "checked": "2026-11-26", "redistribution": "allowed"},
                "endpoints": {},
            },
        ],
    }


def _self_test() -> int:
    fails: list[str] = []

    def ok(cond: bool, msg: str) -> None:
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # Check shipped allowlist loads cleanly
    shipped, _ = load_allowlist(DEFAULT_ALLOWLIST)
    ok(shipped["version"] == 1 and len(shipped["_by_id"]) >= 6, "AQ0 shipped acquire_sources.yaml validates")

    with tempfile.TemporaryDirectory() as td:
        # AQ1: incomplete allowlist fails closed
        aq1_hits = 0
        for label, mutate in (
            ("missing_terms_url", lambda d: d["sources"][0]["licence"].pop("terms_url")),
            ("missing_checked", lambda d: d["sources"][0]["licence"].pop("checked")),
            ("missing_status", lambda d: d["sources"][0].pop("status")),
            ("missing_param_type", lambda d: d["sources"][0]["endpoints"]["slice"]["params"]["code"].pop("type")),
        ):
            bad_doc = _test_allowlist_yaml(80)
            mutate(bad_doc)
            p = os.path.join(td, f"bad_{label}.yaml")
            SIO.write_nofollow(td, f"bad_{label}.yaml", yaml.safe_dump(bad_doc).encode("utf-8"), atomic=True)
            try:
                load_allowlist(p)
            except AllowlistError:
                aq1_hits += 1
        ok(aq1_hits == 4, f"AQ1 allowlist missing terms_url/checked/status/param-type fails closed ({aq1_hits}/4)")

        xxe_xml = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
            b'<root>&lol2;</root>'
        )
        routes = {
            "/api/catalogue": (200, "application/json", json.dumps({"items": [{"code": "IND1"}, {"code": "IND2"}]}).encode(), {}),
            "/api/slice/IND1?mode=ok": (200, "application/json", json.dumps({"data": [{"area": "A1", "val": 12.5}]}).encode(), {"ETag": '"e1"'}),
            "/api/slice/IND1?mode=csv": (200, "text/csv", b"unit,pop,note\nU1,100,free text hidden\nU2,200,other\n", {}),
            "/api/slice/IND1?mode=bad_csv": (200, "text/csv", b"a,b\n1,2\n3,4,5\n", {}),
            "/api/slice/IND1?mode=bad_json": (200, "application/json", b"{not valid json", {}),
            "/api/slice/IND1?mode=html": (200, "application/json", b"<!DOCTYPE html><html><body>502 Error</body></html>", {}),
            "/api/slice/IND1?mode=big": (200, "application/json", b"[" + b"1," * 4000 + b"1]", {}),
            "/api/slice/IND1?mode=redir_host": (302, "text/plain", b"", {"Location": "http://evil.example/api/slice/IND1"}),
            "/api/slice/IND1?mode=redir_ep": (302, "text/plain", b"", {"Location": "/other/path"}),
            "/api/slice/IND1?mode=xxe": (200, "application/xml", xxe_xml, {}),
            "/api/slice/IND1?mode=xml_ok": (200, "application/xml", b"<article><p>efficacy 0.72</p></article>", {}),
            "/api/slice/IND2?mode=ok": (200, "application/json", json.dumps({"data": [{"area": "A2", "val": 9.0}]}).encode(), {}),
            "/lit/search?q=bad+lic": (200, "application/json", json.dumps({"records": [{"id": "1", "licence": "PROPRIETARY"}]}).encode(), {}),
            "/lit/search?q=no+lic": (200, "application/json", json.dumps({"records": [{"id": "1"}]}).encode(), {}),
            "/lit/search?q=mixed+lic": (200, "application/json", json.dumps({"records": [{"id": "1", "licence": "CC-BY-4.0"}, {"id": "2"}]}).encode(), {}),
            "/lit/search?q=good+lic": (200, "application/json", json.dumps({"records": [{"id": "1", "licence": "CC-BY-4.0"}, {"id": "2", "licence": "CC0-1.0"}]}).encode(), {}),
        }

        with AcquireFixtureServer(routes) as srv:
            al_rel = "allowlist.yaml"
            SIO.write_nofollow(td, al_rel, yaml.safe_dump(_test_allowlist_yaml(srv.port)).encode("utf-8"), atomic=True)
            al_path = os.path.join(td, al_rel)
            run1 = os.path.join(td, "run1")
            os.makedirs(run1)

            def _mk_fetcher(rdir: str, **kw) -> Fetcher:
                return Fetcher(
                    rdir, al_path,
                    resolver=lambda h, p: ["127.0.0.1"],
                    _allow_test_http=True, _test_port=srv.port, _allow_loopback_ip=True,
                    **kw,
                )

            F = _mk_fetcher(run1, max_file_bytes=2048)

            # AQ16: requires_key and excluded refused
            aq16_key = aq16_ex = False
            try:
                F.fetch("svc_key", "get", {})
            except FetchRefused:
                aq16_key = True
            try:
                F.fetch("svc_off", "get", {})
            except FetchRefused:
                aq16_ex = True
            ok(aq16_key and aq16_ex, "AQ16 requires_key and excluded sources refused")

            # AQ2b: code not yet observed in catalogue refused
            aq2b = False
            try:
                F.fetch("svc_data", "slice", {"code": "IND1", "mode": "ok"})
            except FetchRefused as e:
                aq2b = e.code == "fetch_param_refused"
            ok(aq2b, "AQ2b coded parameter refused before catalogue fetch")

            # Populate catalogue -> IND1, IND2 now allowed
            F.search("svc_data", "catalogue", {})

            # AQ2: invalid type, full URL as value, unknown code, free text outside terms refused
            aq2_hits = 0
            for bad_p in (
                {"code": "https://evil.com/x"},
                {"code": "bad lower space"},
                {"code": "IND999"},
                {"code": "IND1", "mode": "not_in_enum"},
            ):
                try:
                    F.fetch("svc_data", "slice", bad_p)
                except FetchRefused as e:
                    if e.code == "fetch_param_refused":
                        aq2_hits += 1
            try:
                F.search("svc_lit", "search", {"q": "one two three four five six seven eight nine"})
            except FetchRefused as e:
                if e.code == "fetch_param_refused":
                    aq2_hits += 1
            ok(aq2_hits == 5, f"AQ2 invalid param types, URLs, unknown codes, and >8-word terms refused ({aq2_hits}/5)")

            # AQ3: redirect to other host or other endpoint refused before reading body
            aq3_hits = 0
            for mode in ("redir_host", "redir_ep"):
                try:
                    F.fetch("svc_data", "slice", {"code": "IND1", "mode": mode})
                except FetchRefused as e:
                    if e.code == "fetch_refused":
                        aq3_hits += 1
            ok(aq3_hits == 2, f"AQ3 redirect to off-host or off-endpoint refused ({aq3_hits}/2)")

            # AQ4: DNS rebinding — single resolution + public-IP enforcement + pinned TLS SNI
            # (a) Private IP on first check is refused without connecting
            F_pub = Fetcher(run1, al_path, resolver=lambda h, p: ["10.0.0.1"])
            aq4_priv = False
            try:
                F_pub._resolve_pinned("fixture.local", 443)
            except FetchRefused as e:
                aq4_priv = e.code == "fetch_refused"
            # (b) Rebinding resolver: call 1 returns public IP, call 2 would return private 127.0.0.1;
            # verify _resolve_pinned resolves ONCE and _PinnedHTTPSConnection pins that IP with original SNI host
            calls = []

            def _rebind(h, p):
                calls.append(h)
                return ["93.184.216.34"] if len(calls) == 1 else ["127.0.0.1"]

            F_reb = Fetcher(run1, al_path, resolver=_rebind)
            pinned = F_reb._resolve_pinned("example.org", 443)
            pconn = _PinnedHTTPSConnection("example.org", 443, pinned_ip=pinned, timeout=5.0,
                                           context=ssl.create_default_context())
            ok(aq4_priv and len(calls) == 1 and pinned == "93.184.216.34"
               and pconn._pinned_ip == "93.184.216.34" and pconn.host == "example.org",
               "AQ4 private IP refused, DNS resolved once and pinned while keeping TLS SNI hostname")

            # AQ5: no Authorization/Cookie header, ignores HTTP_PROXY / NETRC
            prev_proxy = os.environ.get("HTTP_PROXY")
            os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
            try:
                srv.requests_seen.clear()
                r_ok = F.fetch("svc_data", "slice", {"code": "IND1", "mode": "ok"})
            finally:
                if prev_proxy is None:
                    os.environ.pop("HTTP_PROXY", None)
                else:
                    os.environ["HTTP_PROXY"] = prev_proxy
            hdrs = srv.requests_seen[-1]["headers"]
            ok("authorization" not in hdrs and "cookie" not in hdrs and r_ok["status"] == "ok",
               "AQ5 no Authorization/Cookie headers and HTTP_PROXY ignored")

            # AQ6: body over cap aborted, .part deleted, record says oversized
            aq6_code = None
            try:
                F.fetch("svc_data", "slice", {"code": "IND1", "mode": "big"})
            except FetchRefused as e:
                aq6_code = e.code
            q_files = SIO.listdir_nofollow(run1, QUARANTINE_DIR)
            ok(aq6_code == "oversized" and not any(x.endswith(".part") for x in q_files),
               f"AQ6 oversized body aborted, .part deleted, record status={aq6_code}")

            # AQ7 & AQ8: HTML-as-JSON, malformed CSV, malformed JSON, XML entity expansion refused at promotion
            r_html = F.fetch("svc_data", "slice", {"code": "IND1", "mode": "html"})
            r_bcsv = F.fetch("svc_data", "slice", {"code": "IND1", "mode": "bad_csv"})
            r_bjsn = F.fetch("svc_data", "slice", {"code": "IND1", "mode": "bad_json"})
            r_xxe = F.fetch("svc_data", "slice", {"code": "IND1", "mode": "xxe"})
            r_csv = F.fetch("svc_data", "slice", {"code": "IND1", "mode": "csv"})
            r_xml = F.fetch("svc_data", "slice", {"code": "IND1", "mode": "xml_ok"})
            res_p = promote(
                run1,
                [r_ok["fetch_id"], r_csv["fetch_id"], r_xml["fetch_id"],
                 r_html["fetch_id"], r_bcsv["fetch_id"], r_bjsn["fetch_id"], r_xxe["fetch_id"]],
                allowlist_path=al_path,
            )
            rej_by_id = {r["fetch_id"]: r["code"] for r in res_p["rejected"]}
            ok(rej_by_id.get(r_html["fetch_id"]) == "acquire_type_mismatch",
               f"AQ7 HTML-as-JSON refused at promotion ({rej_by_id.get(r_html['fetch_id'])})")
            ok(rej_by_id.get(r_bcsv["fetch_id"]) == "acquire_unparseable"
               and rej_by_id.get(r_bjsn["fetch_id"]) == "acquire_unparseable"
               and rej_by_id.get(r_xxe["fetch_id"]) == "acquire_unparseable",
               f"AQ8 malformed CSV/JSON and XML entity expansion refused ({rej_by_id})")

            # AQ10: mode 0444, full provenance entry, sanitized view exists (no free text!), quarantine cleaned
            prov1 = _load_provenance(run1)
            csv_dest = f"{ACQUIRED_ROOT}/svc_data/{r_csv['fetch_id']}.csv"
            csv_entry = prov1["inputs"].get(csv_dest) or {}
            req_fields = {"origin", "sha256", "bytes", "content_type", "url", "retrieved_at", "http",
                          "source_id", "endpoint", "allowlist_version", "allowlist_sha256", "licence",
                          "query", "wanted", "request_digest"}
            view_raw, _ = SIO.read_nofollow(run1, f"{VIEWS_ROOT}/{r_csv['fetch_id']}.schema.json")
            mode_0444 = (os.stat(os.path.join(run1, csv_dest)).st_mode & 0o777) == 0o444
            q_left = SIO.listdir_nofollow(run1, QUARANTINE_DIR)
            ok(mode_0444 and req_fields <= set(csv_entry) and b"free text hidden" not in view_raw and not q_left,
               f"AQ10 promoted file 0444, provenance complete, sanitized view hides cell prose, quarantine empty")

            # AQ15: promotion idempotent
            res_p2 = promote(run1, [r_ok["fetch_id"], r_csv["fetch_id"], r_xml["fetch_id"]], allowlist_path=al_path)
            ok(res_p2["inventory_sha256"] == res_p["inventory_sha256"] and len(res_p2["promoted"]) == 3,
               "AQ15 promote() is idempotent")

            # AQ11: per-record licences (bad, missing, mixed -> rejected; all accepted -> promoted)
            l_bad = F.search("svc_lit", "search", {"q": "bad lic"})
            l_none = F.search("svc_lit", "search", {"q": "no lic"})
            l_mix = F.search("svc_lit", "search", {"q": "mixed lic"})
            l_ok = F.search("svc_lit", "search", {"q": "good lic"})
            res_l = promote(run1, [l_bad["fetch_id"], l_none["fetch_id"], l_mix["fetch_id"], l_ok["fetch_id"]],
                            allowlist_path=al_path)
            rej_l = {r["fetch_id"]: r["code"] for r in res_l["rejected"]}
            ok(rej_l.get(l_bad["fetch_id"]) == "acquire_licence_unclear"
               and rej_l.get(l_none["fetch_id"]) == "acquire_licence_unclear"
               and rej_l.get(l_mix["fetch_id"]) == "acquire_licence_unclear"
               and any(l_ok["fetch_id"] in p for p in res_l["promoted"]),
               f"AQ11 per-record licences reject bad/missing/mixed and accept all-valid ({rej_l})")

            # AQ12: stage_inputs preserves origin: acquired entries and inventory digest
            if REPO not in sys.path:
                sys.path.insert(0, REPO)
            from agents import stage_machine as SM
            prev_inv = _load_provenance(run1)["acquired_inventory_sha256"]
            SM.stage_inputs(run1, "question with no staged files")
            post_prov = _load_provenance(run1)
            ok(csv_dest in post_prov["inputs"] and post_prov.get("acquired_inventory_sha256") == prev_inv,
               "AQ12 stage_inputs preserves origin: acquired entries and acquired_inventory_sha256")

            # AQ18: checkpoint restore resetting mode to 0644 passes verify_inventory and resets back to 0444
            SIO.chmod_nofollow(run1, csv_dest, 0o644)
            v18 = verify_inventory(run1)
            re_0444 = (os.stat(os.path.join(run1, csv_dest)).st_mode & 0o777) == 0o444
            ok(v18["ok"] and csv_dest in v18["restored_modes"] and re_0444,
               "AQ18 checkpoint-restored 0644 file verified by hash and reset to 0444")

            # AQ13: verify_inventory fails on changed, extra, missing, or symlinked file
            aq13_hits = 0
            # extra
            SIO.write_nofollow(run1, f"{ACQUIRED_ROOT}/svc_data/extra.csv", b"x,y\n1,2\n", atomic=True)
            try:
                verify_inventory(run1)
            except ArtifactTampered:
                aq13_hits += 1
            SIO.remove_nofollow(run1, f"{ACQUIRED_ROOT}/svc_data/extra.csv")
            # symlink
            os.symlink(al_path, os.path.join(run1, ACQUIRED_ROOT, "svc_data", "evil_link.csv"))
            try:
                verify_inventory(run1)
            except ArtifactTampered:
                aq13_hits += 1
            SIO.remove_nofollow(run1, f"{ACQUIRED_ROOT}/svc_data/evil_link.csv")
            # changed bytes
            orig_csv, _ = SIO.read_nofollow(run1, csv_dest)
            SIO.chmod_nofollow(run1, csv_dest, 0o644)
            SIO.write_nofollow(run1, csv_dest, orig_csv + b"tamper\n", atomic=True)
            try:
                verify_inventory(run1)
            except ArtifactTampered:
                aq13_hits += 1
            # missing
            SIO.remove_nofollow(run1, csv_dest)
            try:
                verify_inventory(run1)
            except ArtifactTampered:
                aq13_hits += 1
            ok(aq13_hits == 4, f"AQ13 verify_inventory detects extra/symlink/changed/missing ({aq13_hits}/4)")

            # AQ14: replacement Fetcher caches completed request, deletes .part, preserves quotas
            run2 = os.path.join(td, "run2")
            os.makedirs(run2)
            F2 = _mk_fetcher(run2, max_fetches_run=3)
            F2.search("svc_data", "catalogue", {})
            r2a = F2.fetch("svc_data", "slice", {"code": "IND1", "mode": "ok"})
            SIO.write_nofollow(run2, f"{QUARANTINE_DIR}/stale.part", b"partial", atomic=True)
            srv.requests_seen.clear()
            F2_res = _mk_fetcher(run2, max_fetches_run=3)
            r2b = F2_res.fetch("svc_data", "slice", {"code": "IND1", "mode": "ok"})
            r2c = F2_res.fetch("svc_data", "slice", {"code": "IND2", "mode": "ok"})
            aq14_quota = False
            try:
                F2_res.fetch("svc_data", "slice", {"code": "IND1", "mode": "csv"})
            except FetchRefused as e:
                aq14_quota = e.code == "fetch_quota_exceeded"
            ok(r2b["cached"] is True and len(srv.requests_seen) == 1 and r2c["fetch_id"] != r2a["fetch_id"]
               and "stale.part" not in SIO.listdir_nofollow(run2, QUARANTINE_DIR) and aq14_quota,
               "AQ14 replacement fetcher caches completed digest, cleans .part, and enforces persisted quotas")

            # AQ9: tampered quarantined file fails promotion with ArtifactTampered
            SIO.write_nofollow(run2, f"{QUARANTINE_DIR}/{r2a['fetch_id']}.bin", b"CORRUPTED", atomic=True)
            aq9 = False
            try:
                promote(run2, [r2a["fetch_id"]], allowlist_path=al_path)
            except ArtifactTampered:
                aq9 = True
            ok(aq9, "AQ9 tampered quarantined file raises ArtifactTampered at promotion")

            # AQ15b: crash between destination copy and provenance write resumes cleanly
            run3 = os.path.join(td, "run3")
            os.makedirs(run3)
            F3 = _mk_fetcher(run3)
            F3.search("svc_data", "catalogue", {})
            r3 = F3.fetch("svc_data", "slice", {"code": "IND1", "mode": "ok"})
            mid = promote(run3, [r3["fetch_id"]], allowlist_path=al_path, _stop_after_copy=True)
            done = promote(run3, [r3["fetch_id"]], allowlist_path=al_path)
            v3 = verify_inventory(run3)
            ok(mid.get("interrupted") is True and len(done["promoted"]) == 1 and v3["ok"],
               "AQ15b crash between copy and provenance write recovers cleanly via intent")

            # AQ17: acquire/quarantine replaced by symlink -> refused, scratch dir untouched
            run4 = os.path.join(td, "run4")
            scratch = os.path.join(td, "scratch_outside")
            os.makedirs(os.path.join(run4, ACQUIRE_DIR))
            os.makedirs(scratch)
            SIO.write_nofollow(scratch, "keep.txt", b"safe", atomic=True)
            os.symlink(scratch, os.path.join(run4, QUARANTINE_DIR))
            aq17 = False
            try:
                _mk_fetcher(run4)
            except SIO.LinkRefused:
                aq17 = True
            ok(aq17 and os.listdir(scratch) == ["keep.txt"],
               "AQ17 symlinked acquire/quarantine refused with zero writes/deletes outside run")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
