"""Shared upload metadata and staff-status contract for the D1 importer.

These helpers preserve the validated upload identity and status wire formats.
No static archive, migration manifest or Pages publisher is required.
"""
import copy
import hashlib
from pathlib import Path
import re
import unicodedata
from datetime import datetime, timezone
from cloudflare_client import ServiceError

# Compressed upload limit shared with cloudflare/worker.mjs. This is an
# application safeguard, not a D1 database storage allowance.
MAX_SOURCE = 8 * 1024 * 1024
HEX = re.compile(r"[0-9a-f]{64}")


def utc(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (AttributeError, ValueError, TypeError):
        raise ServiceError("invalid_source_timestamp") from None


def upload_metadata(item):
    if not isinstance(item, dict) or not isinstance(item.get("metadata"), dict):
        raise ServiceError("invalid_upload_metadata")
    meta = item["metadata"]
    expected = {"schema_version", "id", "name", "bytes", "sha256", "uploaded_at", "media_type"}
    if set(meta) != expected or meta.get("schema_version") != 1:
        raise ServiceError("invalid_upload_metadata")
    ident, name, digest = meta["id"], meta["name"], meta["sha256"]
    if not all(isinstance(s, str) for s in (ident, name, digest)):
        raise ServiceError("invalid_upload_metadata")
    if not HEX.fullmatch(ident) or not HEX.fullmatch(digest) or item.get("name") != "uploads/" + ident:
        raise ServiceError("invalid_upload_metadata")
    if (not name or len(name.encode("utf-8")) > 220 or name != unicodedata.normalize("NFC", name).strip()
            or re.search(r"[/\\\x00-\x1f\x7f]", name)):
        raise ServiceError("invalid_upload_filename")
    suffix = Path(name).suffix.lower()
    if suffix not in (".csv", ".pdf") or meta["media_type"] != {".csv": "text/csv", ".pdf": "application/pdf"}[suffix]:
        raise ServiceError("invalid_upload_type")
    if type(meta["bytes"]) is not int or not 0 < meta["bytes"] <= MAX_SOURCE:
        raise ServiceError("invalid_upload_size")
    if hashlib.sha256((name + "\n" + digest).encode()).hexdigest() != ident:
        raise ServiceError("invalid_upload_identity")
    utc(meta["uploaded_at"])
    return copy.deepcopy(meta)


def status_record(previous, uploaded, now, *, rejected=(), held=False, message=None):
    accepted = set(previous["files"])
    files = {ident: {"state": "published", "message": "Included in the published database."}
             for ident in accepted}
    for ident in uploaded:
        if ident not in accepted:
            files[ident] = {"state": "stored", "message": message or
                           ("Waiting for another report in this batch to be reviewed." if held else
                            "Waiting for the next daily import.")}
    for ident in rejected:
        files[ident] = {"state": "rejected", "message":
                       (rejected[ident] if isinstance(rejected, dict) else
                        "Transfer file could not be validated. Ask the editor to review its format or totals; the previous public data was kept.")}
    return {"schema_version": 1, "publishing_connected": bool(previous.get("public")),
            "last_run": now, "files": files}
