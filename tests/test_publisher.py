"""Publication safeguards; no network, credentials, or private report fixtures required."""
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cloudflare_client import Cloudflare, NoRedirect, ServiceError, json_bytes
from permit_data import CSV_FIELDS, PARSER_VERSION, parse_report
from publish_permits import (Pages, STATE_KEY, STATUS_KEY, initial_dataset, main,
                             publish_daily, upload_metadata, validate_public, write_site)

NOW = "2026-11-10T13:17:00+00:00"
LATER = "2026-11-11T13:17:00+00:00"


def city(number="B-AC-000001-2026", cost="1234.56"):
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(CSV_FIELDS)
    writer.writerow([number, "Building - Accessory", "Fence", cost, "Fence replacement",
                     "17-12345-100", "123 TEST ST", "TEST OWNER", "TEST CONTRACTOR",
                     "10/1/2026", "10/2/2026"])
    return out.getvalue().encode("utf-8-sig")


class MemoryKV:
    def __init__(self):
        self.objects, self.metadata, self.values, self.writes = {}, {}, {}, []
        self.downloads = 0
        self.fail_key = None

    def upload(self, name="weekly.csv", raw=None, at="2026-11-09T13:00:00Z"):
        raw = city() if raw is None else raw
        digest = hashlib.sha256(raw).hexdigest()
        ident = hashlib.sha256((name + "\n" + digest).encode()).hexdigest()
        meta = {"schema_version": 1, "id": ident, "name": name, "bytes": len(raw),
                "sha256": digest, "uploaded_at": at,
                "media_type": "application/pdf" if name.lower().endswith(".pdf") else "text/csv"}
        self.objects["uploads/" + ident] = raw
        self.metadata[ident] = {"name": "uploads/" + ident, "metadata": meta}
        return ident

    def list_uploads(self):
        return copy.deepcopy(list(self.metadata.values()))

    def get_value(self, key, **_):
        self.downloads += 1
        return self.objects[key]

    def get_json(self, key):
        return copy.deepcopy(self.values.get(key))

    def put_json(self, key, value):
        if key == self.fail_key:
            raise ServiceError("cloudflare_http_429")
        self.writes.append(key)
        self.values[key] = copy.deepcopy(value)


class MemoryPages:
    def __init__(self):
        self.public = None
        self.deployments = 0
        self.fail = False

    def publish(self, folder, data):
        assert {p.name for p in Path(folder).iterdir()} == {"index.html", "permits.json", "_headers"}
        assert json.loads((Path(folder) / "permits.json").read_bytes()) == data
        if self.fail:
            raise ServiceError("pages_deployment_failed")
        self.public = copy.deepcopy(data)
        self.deployments += 1
        return "https://test.pages.dev"


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.kv, self.pages = MemoryKV(), MemoryPages()

    def run_daily(self, **kwargs):
        return publish_daily(self.kv, self.pages, ROOT / "web/index.html", now=kwargs.pop("now", NOW), **kwargs)

    def test_initial_publish_is_curated_and_acknowledged_after_deployment(self):
        ident = self.kv.upload()
        data = self.run_daily()["public"]
        self.assertEqual(data["record_count"], 1)
        self.assertEqual(data["last_checked_at"], NOW)
        self.assertEqual(self.kv.writes, [STATE_KEY, STATUS_KEY])
        self.assertEqual(self.kv.values[STATUS_KEY]["files"][ident]["state"], "published")
        self.assertTrue(self.kv.values[STATUS_KEY]["publishing_connected"])
        self.assertNotIn("uploaded_at", json.dumps(data))
        self.assertNotIn("sha256", json.dumps(data))

    def test_no_change_skips_download_but_publishes_meaningful_check_timestamp(self):
        self.kv.upload()
        first = self.run_daily()["public"]
        result = self.run_daily(now=LATER)
        self.assertFalse(result["changed"])
        self.assertEqual(self.kv.downloads, 1)
        self.assertEqual(result["public"]["generated_at"], first["generated_at"])
        self.assertEqual(result["public"]["last_checked_at"], LATER)

    def test_duplicate_source_does_not_double_permits(self):
        self.kv.upload()
        self.run_daily()
        self.kv.upload("copy.csv", city())
        self.assertEqual(self.run_daily(now=LATER)["public"]["record_count"], 1)

    def test_new_version_updates_permit_and_keeps_other_weeks(self):
        self.kv.upload()
        self.kv.upload("week2.csv", city(number="B-AC-000002-2026"))
        self.run_daily()
        self.kv.upload("weekly.csv", city(cost="9876.54"), at="2026-11-10T15:00:00Z")
        data = self.run_daily(now=LATER)["public"]
        self.assertEqual(data["record_count"], 2)
        self.assertEqual(next(r for r in data["records"] if r["permit_number"].endswith("000001-2026"))["project_cost_cents"], 987654)

    def test_missing_listed_source_does_not_erase_history(self):
        ident = self.kv.upload()
        self.run_daily()
        del self.kv.metadata[ident]
        self.assertEqual(self.run_daily(now=LATER)["public"]["record_count"], 1)

    def test_rejected_report_holds_entire_batch_and_keeps_public_and_state(self):
        original = self.kv.upload()
        self.run_daily()
        before = copy.deepcopy(self.kv.values[STATE_KEY])
        published = copy.deepcopy(self.pages.public)
        good = self.kv.upload("new.csv", city(number="B-AC-000002-2026"))
        bad = self.kv.upload("bad.csv", b"private@email.test,wrong headers\n")
        with self.assertRaisesRegex(ServiceError, "report_validation_failed"):
            self.run_daily(now=LATER)
        self.assertEqual(self.kv.values[STATE_KEY], before)
        self.assertEqual(self.pages.public, published)
        states = self.kv.values[STATUS_KEY]["files"]
        self.assertEqual(states[original]["state"], "published")
        self.assertEqual(states[good]["state"], "stored")
        self.assertEqual(states[bad]["state"], "rejected")
        self.assertNotIn("private@email", json.dumps(states))
        # An editor removes the unaccepted bad upload in KV, then reruns.
        del self.kv.metadata[bad]
        self.assertEqual(self.run_daily(now=LATER)["public"]["record_count"], 2)

    def test_hash_mismatch_never_deploys(self):
        ident = self.kv.upload()
        self.kv.objects["uploads/" + ident] = b"changed"
        with self.assertRaisesRegex(ServiceError, "source_content_hash_mismatch"):
            self.run_daily()
        self.assertEqual(self.pages.deployments, 0)
        self.assertNotIn(STATE_KEY, self.kv.values)

    def test_new_upload_waits_for_consistency_without_partial_publish(self):
        self.kv.upload(at="2026-11-10T13:16:50Z")
        with self.assertRaisesRegex(ServiceError, "recent_upload_pending"):
            self.run_daily()
        self.assertEqual(self.kv.downloads, 0)
        self.assertFalse(self.kv.values[STATUS_KEY]["publishing_connected"])

    def test_deployment_failure_does_not_acknowledge_new_file(self):
        original = self.kv.upload()
        self.run_daily()
        before = copy.deepcopy(self.kv.values[STATE_KEY])
        new = self.kv.upload("new.csv", city(number="B-AC-000002-2026"))
        self.pages.fail = True
        with self.assertRaisesRegex(ServiceError, "pages_deployment_failed"):
            self.run_daily(now=LATER)
        self.assertEqual(before, self.kv.values[STATE_KEY])
        self.assertEqual(self.kv.values[STATUS_KEY]["files"][original]["state"], "published")
        self.assertEqual(self.kv.values[STATUS_KEY]["files"][new]["state"], "stored")

    def test_private_state_write_failure_can_be_retried_idempotently(self):
        ident = self.kv.upload()
        self.kv.fail_key = STATE_KEY
        with self.assertRaisesRegex(ServiceError, "429"):
            self.run_daily()
        self.assertEqual(self.pages.deployments, 1)
        self.assertNotIn(STATUS_KEY, self.kv.values)
        self.kv.fail_key = None
        self.assertEqual(self.run_daily(now=LATER)["public"]["record_count"], 1)
        self.assertEqual(self.kv.values[STATUS_KEY]["files"][ident]["state"], "published")

    def test_parser_upgrade_rechecks_private_original(self):
        ident = self.kv.upload()
        self.run_daily()
        self.kv.values[STATE_KEY]["files"][ident]["parser_version"] = "old"
        self.run_daily(now=LATER)
        self.assertEqual(self.kv.downloads, 2)

    def test_duplicate_upload_timestamp_race_preserves_accepted_precedence(self):
        original = self.kv.upload()
        self.run_daily()
        self.kv.upload("corrected.csv", city(cost="9876.54"), at="2026-11-10T15:00:00Z")
        self.kv.metadata[original]["metadata"]["uploaded_at"] = "2026-11-11T12:00:00Z"
        data = self.run_daily(now=LATER)["public"]
        self.assertEqual(data["records"][0]["project_cost_cents"], 987654)

    def test_bad_metadata_is_a_failure_not_a_silent_skip(self):
        ident = self.kv.upload()
        self.kv.metadata[ident]["metadata"]["sha256"] = "f" * 64
        with self.assertRaisesRegex(ServiceError, "invalid_upload_identity"):
            self.run_daily()
        self.assertEqual(self.pages.deployments, 0)

    def test_contact_content_in_public_fields_is_rejected(self):
        raw = city().replace(b"Fence replacement", b"Call private@email.test")
        self.kv.upload(raw=raw)
        with self.assertRaisesRegex(ServiceError, "report_validation_failed"):
            self.run_daily()
        self.assertEqual(self.pages.deployments, 0)

    def test_starter_is_clear_and_contains_only_static_allowlisted_files(self):
        with tempfile.TemporaryDirectory() as folder:
            write_site(folder, initial_dataset(), ROOT / "web/index.html")
            self.assertEqual({p.name for p in Path(folder).iterdir()}, {"index.html", "permits.json", "_headers"})
            data = json.loads((Path(folder) / "permits.json").read_bytes())
            self.assertEqual(data["import_status"], "awaiting_initial_import")
            self.assertIsNone(data["generated_at"])
            self.assertEqual(data["records"], [])
            with self.assertRaisesRegex(ServiceError, "must_be_empty"):
                write_site(folder, data, ROOT / "web/index.html")

    def test_extra_top_level_private_fields_are_rejected(self):
        data = initial_dataset()
        data["api_token"] = "private"
        with self.assertRaisesRegex(ServiceError, "invalid_public_dataset"):
            validate_public(data)

    def test_no_reports_does_not_publish_a_misleading_empty_database(self):
        with self.assertRaisesRegex(ServiceError, "no_reports_uploaded"):
            self.run_daily()
        self.assertEqual(self.pages.deployments, 0)

    def test_original_two_reports_publish_41_without_contact_blocks(self):
        source = ROOT.parent / "source"
        if not (source / "zoning-2026-10-22-to-28.pdf").exists():
            self.skipTest("Original private reports are not distributed with public code")
        for name in ("Weekly Building Permits 110426.csv", "zoning-2026-10-22-to-28.pdf"):
            self.kv.upload(name, (source / name).read_bytes())
        data = self.run_daily()["public"]
        self.assertEqual(data["record_count"], 41)
        self.assertEqual(sum(r["category"] == "Building" for r in data["records"]), 29)
        county = [r for r in data["records"] if r["category"] == "Zoning"]
        self.assertEqual(sum(r["project_cost_cents"] or 0 for r in county), 13115958)
        self.assertEqual(sum(r["fee_cents"] or 0 for r in county), 285000)
        self.assertNotIn("816 ELM", json.dumps(data))


class Response(io.BytesIO):
    pass


class FakeOpener:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def open(self, req, **_):
        self.requests.append(req)
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return Response(value if isinstance(value, bytes) else json_bytes(value))


class TransportTests(unittest.TestCase):
    def api(self, responses):
        opener = FakeOpener(responses)
        return Cloudflare("a" * 32, "b" * 32, "synthetic-token", opener=opener, sleep=lambda _: None), opener

    def test_paginated_listing_including_empty_cursor_page(self):
        api, opener = self.api([
            {"success": True, "result": [], "result_info": {"cursor": "next"}},
            {"success": True, "result": [{"name": "uploads/one"}], "result_info": {"cursor": ""}},
        ])
        self.assertEqual(api.list_uploads(), [{"name": "uploads/one"}])
        self.assertIn("cursor=next", opener.requests[1].full_url)

    def test_repeated_cursor_stops_infinite_listing(self):
        page = {"success": True, "result": [], "result_info": {"cursor": "same"}}
        api, _ = self.api([page, page])
        with self.assertRaisesRegex(ServiceError, "repeated_upload_cursor"):
            api.list_uploads()

    def test_state_key_is_encoded_and_value_is_raw_json(self):
        api, opener = self.api([{"success": True, "result": {}}])
        api.put_json("control:status", {"schema_version": 1})
        req = opener.requests[0]
        self.assertTrue(req.full_url.endswith("control%3Astatus"))
        self.assertEqual(req.get_header("Content-type"), "application/octet-stream")
        self.assertEqual(json.loads(req.data), {"schema_version": 1})

    def test_missing_state_is_allowed_but_auth_failure_is_not(self):
        api, _ = self.api([HTTPError("url", 404, "missing", {}, None)])
        self.assertIsNone(api.get_json(STATE_KEY))
        api, _ = self.api([HTTPError("url", 403, "secret-detail", {}, None)])
        with self.assertRaisesRegex(ServiceError, "cloudflare_http_403"):
            api.get_json(STATE_KEY)

    def test_429_retries_but_reports_a_safe_failure(self):
        api, opener = self.api([HTTPError("url", 429, "secret-detail", {"Retry-After": "1"}, None) for _ in range(4)])
        with self.assertRaisesRegex(ServiceError, "cloudflare_http_429"):
            api.get_value("key")
        self.assertEqual(len(opener.requests), 4)

    def test_redirect_handler_never_forwards_a_token(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid"))
        api, opener = self.api([HTTPError("url", 302, "redirect", {}, None)])
        with self.assertRaisesRegex(ServiceError, "cloudflare_http_302"):
            api.get_value("key")
        self.assertEqual(len(opener.requests), 1)

    def test_pages_uses_real_hostname_and_production_branch(self):
        api, _ = self.api([{"success": True, "result": {
            "subdomain": "project-actual.pages.dev", "production_branch": "production", "source": None}}])
        self.assertEqual(api.project("project"), {"name": "project", "hostname": "project-actual.pages.dev", "branch": "production"})

    def test_production_verification_failure_never_claims_success(self):
        api, _ = self.api([])
        pages = Pages(api, "project", sleep=lambda _: None)
        opener = FakeOpener([b"old data"] * 6)
        with patch("publish_permits.build_opener", return_value=opener):
            with self.assertRaisesRegex(ServiceError, "verification_failed"):
                pages.verify("project.pages.dev", b"expected")
        self.assertEqual(len(opener.requests), 6)

    def test_production_verification_requires_exact_bytes(self):
        api, _ = self.api([])
        pages = Pages(api, "project", sleep=lambda _: None)
        opener = FakeOpener([b"old", b"expected"])
        with patch("publish_permits.build_opener", return_value=opener):
            pages.verify("project.pages.dev", b"expected")
        self.assertEqual(len(opener.requests), 2)


if __name__ == "__main__":
    unittest.main()
