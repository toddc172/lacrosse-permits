"""Publication safeguards; no network, credentials, or private report fixtures required."""
import copy
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cloudflare_client import Cloudflare, NoRedirect, ServiceError, json_bytes
from permit_data import CSV_FIELDS, PARSER_VERSION, parse_report
from permit_data import PDF_FIELDS, parse_county_pdf, ValidationError
from publish_permits import (Pages, STATE_KEY, STATUS_KEY, initial_dataset, main,
                             PublicSiteRedirect, public_body, same_public_data,
                             publish_daily, upload_metadata, validate_public, write_site)
from publish_permits import private_report_hint

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


class CountyVariantTests(unittest.TestCase):
    def synthetic_county(self, number='4', kind='Sign Permit', work='06-00171-005 / N1936 TEST RD', department='Zoning Department'):
        row = [department, '10/1/2026', '10/2/2026', number, kind, 'Issued',
               'TEST OWNER\n123 MAILING ST', work, '', '', '', 'TEST WORK',
               '', '$0.00', '', '', '', '', '', '']
        total = [''] * 20
        total[0], total[1], total[12], total[13] = 'Total:', '1', '$0.00', '$0.00'
        tables = [
            [list(PDF_FIELDS), row, total],
            [['Count by Type', None, None, None, None],
             ['Type', 'Count', 'Fees', 'Cost Of Construction', 'Sq Feet'],
             [kind, '1', '$0.00', '$0.00', '0'],
             ['Total:', '1', '$0.00', '$0.00', '0']]]
        found = [SimpleNamespace(extract=lambda t=t: t, bbox=(0, 0, 20, 10),
                 rows=[SimpleNamespace(cells=[(i, 0, i+1, 1) for i in range(20)])]) for t in tables]
        page = SimpleNamespace(extract_text=lambda: 'La Crosse County M5 New Permit Report From 10/1/2026 To 10/5/2026',
                               find_tables=lambda: found)
        doc = SimpleNamespace(pages=[page])
        context = unittest.mock.MagicMock()
        context.__enter__.return_value = doc
        with patch('permit_data.remove_clipped_header_wrappers', return_value=(b'test', 0)), patch('permit_data.pdfplumber.open', return_value=context):
            return parse_county_pdf(b'test', 'synthetic.pdf')['records'][0]

    def test_legacy_identifiers_are_preserved_and_normalized_for_matching(self):
        for number in ('ACUP 1295A', 'ACUP-1210Amended', 'ZPT 2090', 'VAR2026-03', 'SE-2026--02', 'AGW 2026-01', 'CU-1298A'):
            self.assertEqual(self.synthetic_county(number=number)['permit_number'], number)
        a = self.synthetic_county(number='ACUP 1295A')
        b = self.synthetic_county(number='ACUP-1295A')
        self.assertEqual(a['id'], b['id'])

    def test_small_numeric_numbers_do_not_merge_different_permit_types(self):
        a = self.synthetic_county(number='4', kind='Sign Permit')
        b = self.synthetic_county(number='4', kind='Short Term Rental')
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual(a['permit_number'], '4')

    def test_missing_source_fields_are_flagged_without_mailing_address_substitution(self):
        row = self.synthetic_county(number='', work='06-00171-005 /')
        self.assertIsNone(row['permit_number'])
        self.assertEqual(row['address'], 'Not reported in source')
        self.assertEqual(row['municipality'], 'Town of Greenfield')
        self.assertNotIn('MAILING', str(row))
        self.assertTrue(row['notes'])
        missing = self.synthetic_county(work='')
        self.assertEqual(missing['municipality'], 'Not reported')

    def test_other_report_departments_are_explicit_and_unknown_departments_fail(self):
        row = self.synthetic_county(number='2026035', kind='Well Permit', department='Health Department')
        self.assertIn('Health Department', ' '.join(row['notes']))
        with self.assertRaises(ValidationError):
            self.synthetic_county(department='Unexpected Department')

    def test_private_review_hints_never_copy_raw_exception_text(self):
        hint = private_report_hint(ValidationError('Contact information needs review: private@email.test'))
        self.assertIn('contact information', hint)
        self.assertNotIn('private@email', hint)

    def test_full_attached_backfill_batch(self):
        expected = {
            'February ZO Permit Report 2.1.26 - 2.28.26.pdf': (17, 342500000, 0),
            'March ZO Permit Report 3.1.26 - 3.31.26.pdf': (36, 524043499, 285000),
            'Weekly ZO Permit Report 1.15.26 - 1.21.26.pdf': (3, 3000000, 0),
            'Weekly ZO Permit Report 11.6.25 - 11.12.25.pdf': (9, 221000000, 55000),
            'Weekly ZO Permit Report 12.18.25 - 1.7.26.pdf': (6, 10852400, 50000),
            'Weekly ZO Permit Report 12.4.25 - 12.10.25.pdf': (4, 38620000, 0),
            'Weekly ZO Permit Report 4.16.26 - 4.22.26.pdf': (19, 279857280, 207500),
            'Weekly ZO Permit Report 4.23.26 - 4.29.26.pdf': (16, 137736999, 0),
            'Weekly ZO Permit Report 4.30.26 - 5.6.26.pdf': (11, 256800000, 50000),
            'Weekly ZO Permit Report 5.14.26 - 5.20.26.pdf': (10, 144319000, 42500),
            'Weekly ZO Permit Report 5.21.26 - 5.27.26.pdf': (13, 54765000, 37500),
            'Weekly ZO Permit Report 5.28.26 - 6.3.26.pdf': (9, 97889999, 50000),
            'Weekly ZO Permit Report 7.2.26 - 7.8.26.pdf': (8, 120120000, 12500),
            'Weekly ZO Permit Report 7.30.26 - 8.5.26.pdf': (6, 25080000, 0),
            'Weekly ZO Permit Report 8.20.26 - 8.26.26.pdf': (9, 72785000, 80000),
            'Weekly ZO Permit Report 8.6.26 - 8.12.26.pdf': (7, 139151500, 0),
            'Weekly ZO Permit Report 9.10.26 - 9.16.26.pdf': (11, 211960053, 160000)}
        if not all((ROOT.parent / 'upload' / name).exists() for name in expected):
            self.skipTest('Private backfill reports are not distributed with public code')
        for name, controls in expected.items():
            with self.subTest(report=name):
                result = parse_report((ROOT.parent / 'upload' / name).read_bytes(), name)
                c = result['controls']
                self.assertEqual((c['count'], c['cost_cents'], c['fee_cents']), controls)

    def test_agricultural_certificate_and_broken_row_borders_reconcile(self):
        source = ROOT.parent / 'upload/Weekly ZO Permit Report 9.17.26 - 9.23.26.pdf'
        if not source.exists():
            self.skipTest('Private September source report is not distributed with public code')
        result = parse_report(source.read_bytes(), source.name)
        records = {r['permit_number']: r for r in result['records']}
        self.assertEqual(len(records), 10)
        self.assertEqual(result['controls']['by_type'],
                         {'Agricultural Certificate': 1, 'Zoning/Occupancy': 9})
        self.assertEqual(result['controls']['cost_cents'], 43150000)
        self.assertEqual(result['controls']['fee_cents'], 62500)
        self.assertEqual(records['AGC-26-0011']['source_pages'], [1, 2])
        self.assertEqual(records['AGC-26-0011']['project_cost_cents'], 10000000)
        repaired = records['ZO-26-0244']
        self.assertEqual(repaired['parcel'], '06-00171-005')
        self.assertEqual(repaired['address'], 'N1936 COUNTY ROAD OA')
        self.assertEqual(repaired['municipality'], 'Town of Greenfield')
        self.assertEqual(repaired['project_cost_cents'], 750000)
        self.assertEqual(repaired['source_pages'], [2])
        self.assertTrue(all(r['contractor'] is None for r in records.values()))


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

    def test_equivalent_json_formatting_is_accepted(self):
        expected = json_bytes({"records": [{"id": "city:1", "cost": 0}], "record_count": 1})
        actual = b'{ "record_count": 1, "records": [ {"cost": 0, "id": "city:1"} ] }\r\n'
        self.assertTrue(same_public_data(actual, expected))
        self.assertFalse(same_public_data(actual.replace(b'"cost": 0', b'"cost": 1'), expected))
        self.assertFalse(same_public_data(actual.replace(b'"cost": 0', b'"cost": false'), expected))

    def test_json_duplicate_keys_and_nonfinite_numbers_are_not_normalized(self):
        self.assertFalse(same_public_data(b'{"count":1,"count":0}', b'{"count":0}'))
        self.assertFalse(same_public_data(b'{"count":NaN}', b'{"count":null}'))

    def test_public_redirects_are_limited_to_same_host_https(self):
        handler = PublicSiteRedirect()
        req = Request('https://project.pages.dev/permits.json', headers={'Accept': 'application/json'})
        next_request = handler.redirect_request(req, None, 302, '', {}, '/permits.json?new=1')
        self.assertEqual(next_request.full_url, 'https://project.pages.dev/permits.json?new=1')
        for target in ('https://login.invalid/', 'http://project.pages.dev/',
                       'https://project.pages.dev:444/', 'https://user@project.pages.dev/'):
            self.assertIsNone(handler.redirect_request(req, None, 302, '', {}, target))
        self.assertIsNone(NoRedirect().redirect_request(req, None, 302, '', {}, '/anything'))

    def test_compressed_public_data_is_checked_after_bounded_decoding(self):
        expected = json_bytes({"record_count": 1, "records": [{"id": "city:1"}]})
        response = Response(gzip.compress(expected))
        response.headers = {'Content-Encoding': 'gzip'}
        self.assertEqual(public_body(response), expected)
        bad = Response(b'not gzip')
        bad.headers = {'Content-Encoding': 'gzip'}
        with self.assertRaisesRegex(ServiceError, 'invalid_compressed_response'):
            public_body(bad)

    def test_verification_diagnostics_classify_errors_without_private_text(self):
        api, _ = self.api([])
        pauses = []
        pages = Pages(api, 'project', sleep=pauses.append)
        errors = [HTTPError('https://project.pages.dev/', 403, 'PRIVATE_RESPONSE', {}, None) for _ in range(6)]
        output = io.StringIO()
        with patch('publish_permits.build_opener', return_value=FakeOpener(errors)), patch('sys.stdout', output):
            with self.assertRaisesRegex(ServiceError, 'verification_failed_http_403'):
                pages.verify('project.pages.dev', b'expected')
        self.assertIn('http_403', output.getvalue())
        self.assertNotIn('PRIVATE_RESPONSE', output.getvalue())
        self.assertEqual(pauses, [5, 10, 20, 30, 30])


if __name__ == "__main__":
    unittest.main()
