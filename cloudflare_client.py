"""Small, bounded Cloudflare REST adapter. No billing or resource creation calls."""
from __future__ import annotations

import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_VALUE = 16 * 1024 * 1024


class ServiceError(RuntimeError):
    """Only fixed, safe error codes may cross into public workflow logs."""

    def __init__(self, code="service_unavailable"):
        self.code = code
        super().__init__(code)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


class NoRedirect(HTTPRedirectHandler):
    # A bearer token must never follow an API redirect to a different host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Cloudflare:
    def __init__(self, account, namespace, token, *, opener=None, sleep=time.sleep):
        if not all(re.fullmatch(r"[0-9a-fA-F]{32}", s or "") for s in (account, namespace)):
            raise ServiceError("invalid_account_or_namespace")
        if not token or any(c.isspace() for c in token):
            raise ServiceError("missing_or_invalid_api_token")
        self.account, self.namespace, self.token = account, namespace, token
        self.opener = opener or build_opener(NoRedirect())
        self.sleep = sleep
        self.base = f"https://api.cloudflare.com/client/v4/accounts/{account}"
        self.kv = f"/storage/kv/namespaces/{namespace}"

    def request(self, path, *, method="GET", data=None, missing_ok=False,
                envelope=True, limit=MAX_VALUE):
        if not path.startswith("/") or path.startswith("//"):
            raise ServiceError("invalid_api_path")
        headers = {"Authorization": "Bearer " + self.token,
                   "User-Agent": "LaCrosseTribune-Permits/1.0",
                   "Accept": "application/json" if envelope else "application/octet-stream"}
        if data is not None:
            headers["Content-Type"] = "application/octet-stream"
        req = Request(self.base + path, headers=headers, method=method, data=data)
        for attempt in range(4):
            try:
                with self.opener.open(req, timeout=30) as response:
                    raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise ServiceError("response_too_large")
                if not envelope:
                    return raw
                result = json.loads(raw)
                if not isinstance(result, dict) or result.get("success") is not True:
                    raise ServiceError("cloudflare_api_rejected")
                return result
            except HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                exc.close()  # Never print an API error body or token.
                if status == 404 and missing_ok:
                    return None
                if status not in (429, 502, 503, 504) or attempt == 3:
                    raise ServiceError(f"cloudflare_http_{status}") from None
                delay = int(retry_after) if retry_after.isdigit() else 2 ** attempt
                self.sleep(min(20, max(1, delay)))
            except (URLError, TimeoutError, OSError):
                if attempt == 3:
                    raise ServiceError("cloudflare_network_unavailable") from None
                self.sleep(2 ** attempt)
            except (ValueError, UnicodeError):
                raise ServiceError("invalid_cloudflare_response") from None
        raise ServiceError()

    def list_uploads(self):
        keys, cursor, seen = {}, "", set()
        for _ in range(100):
            query = {"prefix": "uploads/", "limit": 1000}
            if cursor:
                query["cursor"] = cursor
            response = self.request(self.kv + "/keys?" + urlencode(query))
            page = response.get("result")
            if not isinstance(page, list):
                raise ServiceError("invalid_upload_listing")
            for item in page:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    raise ServiceError("invalid_upload_listing")
                name = item["name"]
                if name in keys and keys[name] != item:
                    raise ServiceError("inconsistent_upload_listing")
                keys[name] = item
            info = response.get("result_info") or {}
            if not isinstance(info, dict):
                raise ServiceError("invalid_upload_cursor")
            cursor = info.get("cursor") or ""
            if not cursor:
                return list(keys.values())
            if not isinstance(cursor, str) or cursor in seen:
                raise ServiceError("repeated_upload_cursor")
            seen.add(cursor)
        raise ServiceError("upload_listing_limit")

    def get_value(self, key, *, missing_ok=False, limit=MAX_VALUE):
        return self.request(self.kv + "/values/" + quote(key, safe=""),
                            envelope=False, missing_ok=missing_ok, limit=limit)

    def get_json(self, key):
        raw = self.get_value(key, missing_ok=True)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise ServiceError("invalid_private_state") from None

    def put_json(self, key, value):
        raw = json_bytes(value)
        if len(raw) > MAX_VALUE:
            raise ServiceError("private_state_capacity")
        self.request(self.kv + "/values/" + quote(key, safe=""), method="PUT", data=raw)

    def project(self, name):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,57}[a-z0-9]|[a-z0-9]", name or ""):
            raise ServiceError("invalid_pages_project")
        data = self.request("/pages/projects/" + quote(name, safe="")).get("result")
        if not isinstance(data, dict):
            raise ServiceError("invalid_pages_project")
        host, branch = data.get("subdomain", ""), data.get("production_branch", "")
        if not re.fullmatch(r"[a-z0-9-]+\.pages\.dev", host) or not branch or len(branch) > 200:
            raise ServiceError("invalid_pages_project")
        # This application only deploys static files, to an already-created project.
        if data.get("source"):
            raise ServiceError("use_a_direct_upload_pages_project")
        return {"name": name, "hostname": host, "branch": branch}
