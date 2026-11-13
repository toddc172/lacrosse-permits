"""DOR export adapter. Raw statewide reports remain private compressed KV values."""
import csv
import decimal
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
from datetime import datetime
from parse_retr import COUNTIES, MAPPING, MONEY, NUMBERS, PLACEHOLDERS, numeric

PARSER_VERSION = 'transfers-1.0.0'
HEADERS = json.loads(Path(__file__).with_name('schema_baseline.json').read_text())['source_headers']
FIELDS = {d for d, _ in MAPPING} | {'id', 'source_month', 'property_address', 'address_status', 'review_flags'}


def parse_report(raw, filename):
    match = re.fullmatch(r'RETRHistoricalReport(\d{4})(\d{2})\.csv', filename)
    if not match:
        raise ValueError('filename must be RETRHistoricalReportYYYYMM.csv')
    month = match[1] + '-' + match[2]
    datetime.strptime(month, '%Y-%m')
    # Bound the uncompressed source, even when a small gzip expands excessively.
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        source = stream.read(64 * 1024 * 1024 + 1)
    if len(source) > 64 * 1024 * 1024:
        raise ValueError('source capacity exceeded')
    reader = csv.reader(io.StringIO(source.decode('utf-8-sig'), newline=''))
    if next(reader, []) != HEADERS:
        raise ValueError('source headers changed')
    records, total = [], 0
    for values in reader:
        total += 1
        if len(values) != len(HEADERS):
            raise ValueError('source column count mismatch')
        src = dict(zip(HEADERS, values))
        # Validate batch dates statewide before filtering geography.
        recorded = datetime.strptime(src['Recorded Date'].strip(), '%m-%d-%Y').date().isoformat()
        if recorded[:7] != month:
            raise ValueError('recorded date outside source period')
        if src['County'].strip() not in COUNTIES:
            continue
        row = {dest: src[field].strip() for dest, field in MAPPING}
        for field in ['recorded_date', 'conveyance_date']:
            row[field] = datetime.strptime(row[field], '%m-%d-%Y').date().isoformat() if row[field] else ''
        for field in MONEY | NUMBERS:
            row[field] = numeric(row[field], money=field in MONEY)
        if not row['document_number'] or not row['parcel_number']:
            raise ValueError('missing source identifier')
        row['id'] = re.sub(r'\s+', '_', row['county'].lower()) + ':' + row['document_number']
        row['source_month'] = month
        row['property_address'] = '' if row['property_address_reported'].upper() in PLACEHOLDERS else row['property_address_reported']
        row['address_status'] = 'reported_unverified' if row['property_address'] else 'placeholder_or_missing'
        flags = []
        if not row['property_address']: flags.append('address_placeholder_or_missing')
        if not row['grantee_name']: flags.append('missing_grantee')
        if row['conveyance_type'] != 'Sale': flags.append('not_labeled_sale')
        if row['sale_price'] == '0.00': flags.append('zero_reported_sale_price')
        if row['conveyance_type'] == 'Sale' and row['sale_price'] == '0.00': flags.append('reported_sale_zero_price')
        if row['ownership_type'] != 'Full': flags.append('ownership_not_full')
        if row['part_of_parcel_transferred'] != '1. Entire parcel': flags.append('partial_parcel_or_improvements')
        if row['document_type'] == 'Affidavit of correction/correction instrument' or row['fee_exemption'].startswith('3 - '): flags.append('correction_related')
        if row['party_relationship'] != 'No relationship': flags.append('relationship_requires_review')
        row['review_flags'] = ';'.join(flags)
        records.append(row)
    if not records:
        raise ValueError('no selected county records')
    validate_unique(records)
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'parser_version': PARSER_VERSION,
            'records': records, 'controls': {'source_month': month, 'statewide_rows': total,
            'selected_rows': len(records), 'county_counts': {c: sum(r['county'] == c for r in records) for c in COUNTIES}}}


def validate_unique(records):
    seen = set()
    for row in records:
        if set(row) != FIELDS or any(not isinstance(v, str) for v in row.values()):
            raise ValueError('unexpected public fields')
        if row['county'] not in COUNTIES or row['id'] in seen:
            raise ValueError('duplicate or invalid transfer identifier')
        if row['id'] != re.sub(r'\s+', '_', row['county'].lower()) + ':' + row['document_number']:
            raise ValueError('invalid transfer identifier')
        for field in MONEY | NUMBERS:
            if row[field] and numeric(row[field], money=field in MONEY) != row[field]:
                raise ValueError('invalid numeric field')
        seen.add(row['id'])


def public_dataset(files, generated_at):
    months, digests, records = {}, set(), []
    for entry in files.values():
        digest = entry['metadata']['sha256']
        if digest in digests:
            continue
        digests.add(digest)
        month = entry['controls']['source_month']
        if month in months:
            raise ValueError('changed monthly file requires editorial replacement review')
        months[month] = True
        records.extend(entry['records'])
    # Never silently resolve document conflicts by upload timestamp.
    validate_unique(records)
    records.sort(key=lambda r: (r['recorded_date'], r['county'], r['document_number']), reverse=True)
    return {'schema_version': 1, 'generated_at': generated_at, 'record_count': len(records),
            'source_count': len(months), 'included_months': sorted(months), 'records': records,
            'latest_recorded_date': max((r['recorded_date'] for r in records), default=None),
            'methodology': 'Wisconsin DOR RETR exports for six counties. Includes all transfer types. Sale Price and Estimated Value remain separate. Zero prices are retained. Grantor/grantee names and property locations are as reported; locations are not independently verified. Only listed source months are included.'}
