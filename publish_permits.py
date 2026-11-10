"""Daily private-KV import -> validated static Pages deployment.

Run exactly one publishing workflow, with concurrency cancellation disabled.
KV has eventual consistency and is NOT a transactional lock or CAS store.
No raw source is written to disk or to the public repository.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from cloudflare_client import Cloudflare, MAX_VALUE, NoRedirect, ServiceError, json_bytes
from permit_data import PARSER_VERSION, parse_report, public_dataset, validate_unique

STATE_KEY = "state:current"
STATUS_KEY = "control:status"
MAX_SOURCE = 2 * 1024 * 1024
WRANGLER = "4.159.0"
HEX = re.compile(r"[0-9a-f]{64}")
PUBLIC_KEYS = {"schema_version", "generated_at", "last_checked_at", "latest_issue_date",
               "record_count", "source_count", "records", "municipality_source",
               "methodology", "import_status"}


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


def validate_public(data):
    if (not isinstance(data, dict) or set(data) - PUBLIC_KEYS or data.get("schema_version") != 1
            or not isinstance(data.get("records"), list)
            or data.get("record_count") != len(data["records"])):
        raise ServiceError("invalid_public_dataset")
    try:
        validate_unique(data["records"])
    except Exception:
        raise ServiceError("public_field_validation_failed") from None
    if len(json_bytes(data)) > MAX_VALUE:
        raise ServiceError("public_dataset_capacity")


def initial_dataset():
    result = public_dataset({}, generated_at="1970-01-01T00:00:00Z")
    result.update(generated_at=None, last_checked_at=None, import_status="awaiting_initial_import")
    return result


def write_site(folder, data, html_path):
    """Allowlist the entire deployment. Never recursively copy a work directory."""
    validate_public(data)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if any(folder.iterdir()):
        raise ServiceError("site_directory_must_be_empty")
    html = Path(html_path).read_text(encoding="utf-8")
    if 'id="preview-data"' in html:
        raise ServiceError("preview_data_in_production_html")
    scripts = re.findall(r"<script>([\s\S]*?)</script>", html)
    if len(scripts) != 1 or re.search(r"<script[^>]+src=", html, re.I):
        raise ServiceError("unexpected_production_script")
    script_hash = base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
    headers = ("/*\n"
               "  Content-Security-Policy: default-src 'none'; script-src 'sha256-" + script_hash +
               "'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
               "font-src 'self'; base-uri 'none'; form-action 'none'; object-src 'none'; "
               "frame-ancestors 'self' https://lacrossetribune.com https://*.lacrossetribune.com\n"
               "  X-Content-Type-Options: nosniff\n"
               "  Referrer-Policy: no-referrer\n"
               "  Permissions-Policy: camera=(), microphone=(), geolocation=()\n"
               "  Cache-Control: no-cache\n\n"
               "/permits.json\n  Cache-Control: no-store\n")
    (folder / "index.html").write_text(html, encoding="utf-8")
    (folder / "permits.json").write_bytes(json_bytes(data))
    (folder / "_headers").write_text(headers, encoding="utf-8")


class Pages:
    def __init__(self, api, project_name, *, sleep=time.sleep):
        self.api, self.project_name, self.sleep = api, project_name, sleep

    def publish(self, folder, data):
        project = self.api.project(self.project_name)
        # subprocess arguments, never a shell command assembled from input.
        env = dict(os.environ, CLOUDFLARE_API_TOKEN=self.api.token,
                   CLOUDFLARE_ACCOUNT_ID=self.api.account, CI="true",
                   WRANGLER_SEND_METRICS="false")
        try:
            completed = subprocess.run(
                ["npx", "--yes", "wrangler@" + WRANGLER, "pages", "deploy", str(folder),
                 "--project-name", project["name"], "--branch", project["branch"],
                 "--commit-dirty=true"], env=env, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=360, check=False,
                # No repository wrangler config or private sources in this directory.
                cwd=str(folder))
        except (OSError, subprocess.TimeoutExpired):
            raise ServiceError("pages_deployment_unavailable") from None
        if completed.returncode:
            raise ServiceError("pages_deployment_failed")
        self.verify(project["hostname"], json_bytes(data))
        return "https://" + project["hostname"]

    def verify(self, hostname, expected):
        digest = hashlib.sha256(expected).hexdigest()
        url = "https://" + hostname + "/permits.json?publish=" + digest
        opener = build_opener(NoRedirect())
        for attempt in range(6):
            try:
                req = Request(url, headers={"Cache-Control": "no-cache"})
                with opener.open(req, timeout=20) as response:
                    raw = response.read(MAX_VALUE + 1)
                if raw == expected:
                    return
            except (HTTPError, URLError, OSError, TimeoutError):
                pass
            if attempt < 5:
                self.sleep(min(10, 2 ** attempt))
        raise ServiceError("pages_production_verification_failed")


def read_state(api):
    state = api.get_json(STATE_KEY)
    if state is None:
        return {"schema_version": 1, "files": {}, "public": None}
    if (not isinstance(state, dict) or state.get("schema_version") != 1
            or not isinstance(state.get("files"), dict)):
        raise ServiceError("invalid_private_state")
    for ident, entry in state["files"].items():
        if not isinstance(entry, dict) or ident != entry.get("metadata", {}).get("id"):
            raise ServiceError("invalid_private_state")
        upload_metadata({"name": "uploads/" + ident, "metadata": entry["metadata"]})
        if entry.get("modified") != entry["metadata"]["uploaded_at"] or not isinstance(entry.get("records"), list):
            raise ServiceError("invalid_private_state")
        try:
            validate_unique(entry["records"])
        except Exception:
            raise ServiceError("invalid_private_state") from None
    if state.get("public") is not None:
        validate_public(state["public"])
    return state


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
                       "Report could not be validated. Ask the editor to review its format or totals; the previous public data was kept."}
    return {"schema_version": 1, "publishing_connected": bool(previous.get("public")),
            "last_run": now, "files": files}


def publish_daily(api, pages, html_path, *, now=None, parser=parse_report):
    now = now or datetime.now(timezone.utc).isoformat()
    clock = utc(now)
    previous = read_state(api)
    uploaded = {}
    for item in api.list_uploads():
        meta = upload_metadata(item)
        if utc(meta["uploaded_at"]) > clock:
            raise ServiceError("source_timestamp_in_future")
        if meta["id"] in uploaded and uploaded[meta["id"]] != meta:
            raise ServiceError("inconsistent_upload_listing")
        uploaded[meta["id"]] = meta
    files = copy.deepcopy(previous["files"])
    rejected, pending = [], []
    # Cached originals may be missing from an eventually consistent listing.
    # Retain history; reparse the private immutable original after parser changes.
    wanted = {ident: entry["metadata"] for ident, entry in files.items()}
    wanted.update(uploaded)
    for ident, meta in sorted(wanted.items(), key=lambda pair: (pair[1]["uploaded_at"], pair[0])):
        prior = files.get(ident)
        if prior:
            # Two near-simultaneous duplicate uploads can race in eventual KV.
            # Their bytes/id are identical, but the last metadata write may carry
            # a later upload time. Retain the first accepted source precedence.
            identity = lambda m: {k: v for k, v in m.items() if k != "uploaded_at"}
            if identity(prior["metadata"]) != identity(meta):
                raise ServiceError("immutable_source_metadata_changed")
            meta = prior["metadata"]
        if prior and prior.get("parser_version") == PARSER_VERSION:
            continue
        if (clock - utc(meta["uploaded_at"])).total_seconds() < 90:
            pending.append(ident)
            continue
        try:
            raw = api.get_value("uploads/" + ident, limit=MAX_SOURCE)
            if len(raw) != meta["bytes"] or hashlib.sha256(raw).hexdigest() != meta["sha256"]:
                raise ServiceError("source_content_hash_mismatch")
            parsed = parser(raw, meta["name"])
            if parsed["sha256"] != meta["sha256"] or parsed["parser_version"] != PARSER_VERSION:
                raise ServiceError("invalid_parser_result")
            validate_unique(parsed["records"])
            files[ident] = {"metadata": meta, "modified": meta["uploaded_at"],
                            "parser_version": PARSER_VERSION, "records": parsed["records"],
                            "controls": parsed["controls"]}
        except ServiceError:
            # Transport/quota errors are retryable failures, not bad-report diagnoses.
            api.put_json(STATUS_KEY, status_record(previous, uploaded, now,
                         message="Import was interrupted. The previous public data was kept; retry the daily workflow."))
            raise
        except Exception:
            # Parser exceptions can contain private contact blocks. Never expose them.
            rejected.append(ident)
    if rejected or pending:
        api.put_json(STATUS_KEY, status_record(previous, uploaded, now,
                     rejected=rejected, held=True, message=(
                         "A recent upload is still settling. Run the daily import again in a few minutes." if pending and not rejected else None)))
        raise ServiceError("report_validation_failed" if rejected else "recent_upload_pending")
    if not files:
        api.put_json(STATUS_KEY, status_record(previous, uploaded, now))
        raise ServiceError("no_reports_uploaded")
    # Each upload version is a historical source. Latest upload wins per permit ID.
    # An omitted permit is NOT an automatic deletion of historical public records.
    data = public_dataset(files, generated_at=now)
    prior_public = previous.get("public")
    changed = not prior_public or any(data.get(k) != prior_public.get(k)
                                      for k in ("records", "source_count", "methodology"))
    if not changed:
        data["generated_at"] = prior_public["generated_at"]
    data.update(last_checked_at=now, import_status="published")
    validate_public(data)
    state = {"schema_version": 1, "files": files, "public": data}
    if len(json_bytes(state)) > MAX_VALUE:
        raise ServiceError("private_state_capacity")
    try:
        with tempfile.TemporaryDirectory(prefix="permits-site-") as temp:
            folder = Path(temp).resolve()
            write_site(folder, data, html_path)
            public_url = pages.publish(folder, data)
    except Exception:
        api.put_json(STATUS_KEY, status_record(previous, uploaded, now,
                     message="Publication was interrupted. No new upload was marked published; retry the daily workflow."))
        raise
    # Only acknowledge after the exact production JSON is verified. If either KV
    # write fails, retries are safe because every original is immutable and retained.
    api.put_json(STATE_KEY, state)
    api.put_json(STATUS_KEY, status_record(state, uploaded, now))
    return {"public": data, "public_url": public_url, "changed": changed}


def main():
    args = argparse.ArgumentParser()
    args.add_argument("--public-output", default="public/permits.json")
    args.add_argument("--starter", help="Build an empty static starter folder; no credentials needed.")
    opts = args.parse_args()
    root = Path(__file__).resolve().parent
    if opts.starter:
        write_site(opts.starter, initial_dataset(), root / "web/index.html")
        return 0
    try:
        # Enforce the free standard-runner/public-repository design.
        if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("PERMITS_PUBLIC_REPOSITORY") != "true":
            raise ServiceError("run_only_in_the_public_repository_workflow")
        api = Cloudflare(os.environ.get("CLOUDFLARE_ACCOUNT_ID", ""),
                         os.environ.get("CLOUDFLARE_KV_NAMESPACE_ID", ""),
                         os.environ.get("CLOUDFLARE_API_TOKEN", ""))
        pages = Pages(api, os.environ.get("CLOUDFLARE_PAGES_PROJECT", ""))
        result = publish_daily(api, pages, root / "web/index.html")
        target = Path(opts.public_output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(json_bytes(result["public"]))
        # Only counts and fixed messages go to the public workflow log.
        print(f"Published {result['public']['record_count']} validated permit records.")
        return 0
    except Exception as exc:
        print("Daily import stopped: " + (exc.code if isinstance(exc, ServiceError) else "review_required"), file=sys.stderr)
        print("No new upload was marked published before production verification.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
