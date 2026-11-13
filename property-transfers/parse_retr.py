#!/usr/bin/env python3
"""Parse the validated 78-column RETR export schema; compare other schemas before importing.
Python standard library only. Builds a monthly snapshot for the supplied source month; preserves raw values.
"""
import argparse
import collections
import csv
import datetime as dt
import decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3

COUNTIES = ['La Crosse', 'Jackson', 'Trempealeau', 'Monroe', 'Vernon', 'Buffalo']
MAPPING = [
    ('county', 'County'), ('document_number', 'Document Number'),
    ('recorded_date', 'Recorded Date'), ('conveyance_date', 'Conveyance Date'),
    ('municipality', 'Municipality'), ('parcel_number', 'Parcel Number'),
    ('property_address_reported', 'Physical Address'),
    ('grantor_name', 'Grantor Name'), ('grantee_name', 'Grantee Name'),
    ('grantor_type', 'Grantor Type'), ('grantee_type', 'Grantee Type'),
    ('document_type', 'Document Type'), ('document_explanation', 'Document Explain'),
    ('conveyance_type', 'Conveyance Type'), ('conveyance_explanation', 'Conveyance Explain'),
    ('party_relationship', 'Grantor/Grantee Relationship'),
    ('relationship_explanation', 'Grantor/Grantee Explain'),
    ('ownership_type', 'Ownership Type'), ('part_of_parcel_transferred', 'Part of Parcel Transferred'),
    ('rights_retained', 'Rights Retained by Grantor'),
    ('property_type', 'Property Type'), ('property_use', 'Property Use Type'),
    ('property_use_subtype', 'Property Use Sub Type'), ('property_explanation', 'Property Explain'),
    ('number_of_units', 'Number of Units'), ('acres', 'Acres'), ('square_feet', 'Square Feet'),
    ('water_frontage_feet', 'Feet of Water Frontage'),
    ('sale_price', 'Sale Price'), ('estimated_value', 'Estimated Value'),
    ('fee_exemption', 'Fee Exemption'), ('transfer_fee_due', 'Transfer Fee Due'),
    ('personal_property_excluded', 'Personal Property Excluded'),
    ('personal_property_included', 'Personal Property Included'),
    ('previous_document_number', 'Previous Document Number'),
]
MONEY = {'sale_price', 'estimated_value', 'transfer_fee_due',
         'personal_property_excluded', 'personal_property_included'}
NUMBERS = {'number_of_units', 'acres', 'square_feet', 'water_frontage_feet'}
PLACEHOLDERS = {'', 'NONE', 'N/A', 'NA', 'NO ADDRESS LISTED', 'NO ADDRESS',
                'UNKNOWN', 'NOT AVAILABLE', 'VACANT LAND', '0'}


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f)
        header = next(reader)
        if len(set(header)) != len(header):
            raise ValueError('Duplicate source headers; review required.')
        records = []
        for rownum, values in enumerate(reader, 2):
            if len(values) != len(header):
                raise ValueError(f'CSV record {rownum}: wrong field count')
            records.append((rownum, dict(zip(header, values))))
    return header, records


def write_csv(path, fields, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def numeric(value, money=False):
    value = value.strip()
    if not value:
        return ''
    cleaned = value.replace('$', '').replace(',', '')
    number = decimal.Decimal(cleaned)
    if not number.is_finite():
        raise ValueError(f'Nonfinite number: {value!r}')
    if money:
        if number != number.quantize(decimal.Decimal('.01')):
            raise ValueError(f'Unexpected fractional cent: {value!r}')
        return format(number, '.2f')
    return format(number, 'f')


def parse_month(value):
    dt.datetime.strptime(value, '%Y-%m')
    return value


def build(path, out, month, baseline):
    header, all_records = read_csv(path)
    expected = json.loads(baseline.read_text())['source_headers']
    if set(header) != set(expected):
        raise ValueError('Schema changed. Run --compare and review mappings before importing.')
    selected = [(n, r) for n, r in all_records if r['County'].strip() in COUNTIES]
    cleaned, seen = [], set()
    for n, raw in selected:
        row = {dest: raw[src].strip() for dest, src in MAPPING}
        for field in ['recorded_date', 'conveyance_date']:
            row[field] = dt.datetime.strptime(row[field], '%m-%d-%Y').date().isoformat() if row[field] else ''
        for field in MONEY:
            row[field] = numeric(row[field], money=True)
        for field in NUMBERS:
            row[field] = numeric(row[field])
        if not row['document_number']:
            raise ValueError(f'CSV record {n}: missing document number')
        row['transfer_id'] = re.sub(r'\s+', '_', row['county'].lower()) + ':' + row['document_number']
        if row['transfer_id'] in seen:
            raise ValueError('Repeated county/document key; preserve and review source rows before proceeding.')
        seen.add(row['transfer_id'])
        address = row['property_address_reported']
        row['property_address'] = '' if address.upper() in PLACEHOLDERS else address
        row['address_status'] = 'placeholder_or_missing' if not row['property_address'] else 'reported_unverified'
        match = re.fullmatch(r'(.+), (City|Village|Town) of', row['municipality'])
        row['municipality_name'] = match[1] if match else row['municipality']
        row['municipality_type'] = match[2] if match else ''
        row['is_reported_sale'] = '1' if row['conveyance_type'] == 'Sale' else '0'
        flags = []
        if row['is_reported_sale'] == '0': flags.append('not_labeled_sale')
        if row['sale_price'] == '0.00': flags.append('zero_reported_sale_price')
        if row['sale_price'] == '0.00' and row['is_reported_sale'] == '1': flags.append('reported_sale_zero_price')
        if not row['property_address']: flags.append('address_placeholder_or_missing')
        if not row['grantor_name']: flags.append('missing_grantor')
        if not row['grantee_name']: flags.append('missing_grantee')
        if row['party_relationship'] != 'No relationship': flags.append('relationship_requires_review')
        if row['ownership_type'] != 'Full': flags.append('ownership_not_full')
        if row['part_of_parcel_transferred'] != '1. Entire parcel': flags.append('less_than_entire_parcel')
        if row['document_type'] == 'Affidavit of correction/correction instrument' or row['fee_exemption'].startswith('3 - '): flags.append('correction_related')
        if row['recorded_date'][:7] != month: flags.append('recorded_outside_source_month')
        if row['conveyance_date'] and row['recorded_date'] and row['conveyance_date'] > row['recorded_date']: flags.append('conveyance_after_recording')
        if ';' in address: flags.append('address_contains_semicolon')
        row['review_flags'] = ';'.join(flags)
        row.update(source_file=path.name, source_month=month, source_record_number=str(n), schema_version='retr_export_78_v1')
        cleaned.append(row)
    cleaned.sort(key=lambda r: (COUNTIES.index(r['county']), r['recorded_date'], r['document_number']))
    out.mkdir(parents=True, exist_ok=True)
    fields = ['transfer_id'] + [d for d, _ in MAPPING] + [
        'property_address', 'address_status', 'municipality_name', 'municipality_type',
        'is_reported_sale', 'review_flags', 'source_file', 'source_month', 'source_record_number', 'schema_version']
    write_csv(out / f'transfers_{month}_six_counties.csv', fields, cleaned)
    write_csv(out / f'source_{month}_six_counties.csv', header, [r for _, r in selected])
    profile = []
    for i, field in enumerate(header, 1):
        values = [r[field] for _, r in selected]
        profile.append({'source_position': i, 'source_field': field,
                        'import_field': next((d for d, s in MAPPING if s == field), ''),
                        'blank_after_trim': sum(not v.strip() for v in values),
                        'distinct_values_after_trim': len({v.strip() for v in values}),
                        'retained_in_raw': 'yes'})
    write_csv(out / 'source_field_profile.csv', list(profile[0]), profile)
    guides = []
    for field in fields:
        src = next((s for d, s in MAPPING if d == field), '')
        kind = 'decimal dollars; zero retained' if field in MONEY else 'decimal number' if field in NUMBERS else 'ISO date YYYY-MM-DD' if field.endswith('_date') else 'text'
        notes = {
            'transfer_id': 'Provisional county + document key; unique in this snapshot; validate across older files before merging.',
            'parcel_number': 'Text; remove surrounding whitespace/tab only; preserve leading zeros and hyphens. Source provides one parcel field; do not assume a complete parcel list.',
            'grantor_name': 'Transferor as reported; may be a person, trust, entity or combined name. Not necessarily seller. Do not split on commas or AND.',
            'grantee_name': 'Transferee as reported; may be blank. Not necessarily buyer. Source completeness of all parties is not independently verified.',
            'sale_price': 'Source Sale Price only. Never replace zero or blank with estimated value; not assessed value.',
            'estimated_value': 'Separate source Estimated Value field; interpretation not independently confirmed; never label this sale price.',
            'property_address_reported': 'Trimmed source Physical Address, including placeholders and descriptions; not geocoded or independently verified.',
            'property_address': 'Reported address except explicit placeholder values become blank. Some remaining values are road names, descriptions or multiple addresses.',
            'municipality': 'Legal municipality as reported. Town, village and city of the same name remain distinct; not verified postal city.',
            'municipality_name': 'Derived only from exact NAME, City/Village/Town of syntax; not postal city.',
            'municipality_type': 'Derived legal municipality type. Empty if source does not follow known pattern.',
            'is_reported_sale': '1 only when source Conveyance Type equals Sale. Not a certification of an arm\u2019s-length sale.',
            'review_flags': 'Semicolon-separated flags for filtering/editorial review; no flagged records excluded.',
            'source_month': 'Requested source batch month, not conveyance month. Recorded-date consistency checked separately.',
            'source_record_number': 'One-based logical CSV record number including header; not physical line number.',
            'square_feet': 'Source field meaning not verified; do not present as building floor area or use for price per square foot.',
            'acres': 'Source-reported acreage; do not infer complete transferred area without validation.',
            'recorded_date': 'Use for monthly recorded-transfer grouping.',
            'conveyance_date': 'Keep separate from recorded date; source may report a much earlier date.',
        }.get(field, 'Direct source value, trimmed; numeric/date transformations as listed.' if src else 'Derived or provenance field.')
        guides.append({'import_field': field, 'source_field': src, 'format': kind, 'notes': notes})
    write_csv(out / 'import_field_guide.csv', list(guides[0]), guides)
    flags = collections.Counter(flag for row in cleaned for flag in row['review_flags'].split(';') if flag)
    county_stats = []
    for county in COUNTIES:
        rr = [r for r in cleaned if r['county'] == county]
        county_stats.append({'county': county, 'transfers': len(rr),
            'labeled_sale': sum(r['is_reported_sale'] == '1' for r in rr),
            'not_labeled_sale': sum(r['is_reported_sale'] == '0' for r in rr),
            'zero_sale_price': sum(r['sale_price'] == '0.00' for r in rr),
            'address_placeholder': sum(not r['property_address'] for r in rr)})
    write_csv(out / 'county_summary.csv', list(county_stats[0]), county_stats)
    summary = {'source_file': path.name, 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'source_columns': len(header), 'statewide_source_rows': len(all_records), 'selected_rows': len(cleaned),
        'unique_county_document_keys': len(seen), 'county_summary': county_stats, 'flag_counts': dict(flags),
        'recorded_date_min': min(r['recorded_date'] for r in cleaned),
        'recorded_date_max': max(r['recorded_date'] for r in cleaned),
        'conveyance_date_min': min(r['conveyance_date'] for r in cleaned if r['conveyance_date']),
        'conveyance_date_max': max(r['conveyance_date'] for r in cleaned if r['conveyance_date']),
        'conveyed_before_source_month': sum(r['conveyance_date'][:7] < month for r in cleaned if r['conveyance_date']),
        'conveyance_type_counts': dict(collections.Counter(r['conveyance_type'] for r in cleaned)),
        'schema_comparison_status': '78-column export schema; value labels retained as reported. Cross-batch keys must be checked before merging.'}
    (out / 'validation_summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    db_path = out / f'property_transfers_{month}.sqlite'
    if db_path.exists():
        raise ValueError('Output database exists; choose a fresh output directory.')
    with sqlite3.connect(db_path) as db:
        definitions = ','.join('"' + field + '" TEXT' + (' PRIMARY KEY' if field == 'transfer_id' else '') for field in fields)
        db.execute('CREATE TABLE transfers (' + definitions + ', sale_price_cents INTEGER, estimated_value_cents INTEGER)')
        for row in cleaned:
            cents = [int(decimal.Decimal(row[x]) * 100) if row[x] else None for x in ['sale_price', 'estimated_value']]
            db.execute('INSERT INTO transfers VALUES (' + ','.join('?' for _ in range(len(fields)+2)) + ')', [row[f] for f in fields] + cents)
        db.execute('CREATE TABLE source_records (transfer_id TEXT PRIMARY KEY REFERENCES transfers(transfer_id), source_record_number INTEGER, raw_json TEXT)')
        by_record = {int(r['source_record_number']): r['transfer_id'] for r in cleaned}
        db.executemany('INSERT INTO source_records VALUES (?,?,?)', [(by_record[n], n, json.dumps(r, ensure_ascii=False)) for n, r in selected])
        db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)')
        db.executemany('INSERT INTO metadata VALUES (?,?)', [('validation_summary', json.dumps(summary)), ('source_headers', json.dumps(header))])
        for col in ['county', 'recorded_date', 'municipality', 'parcel_number', 'conveyance_type', 'sale_price_cents']:
            db.execute(f'CREATE INDEX idx_{col} ON transfers ({col})')
        db.execute("CREATE VIRTUAL TABLE transfer_search USING fts5(transfer_id UNINDEXED, county, municipality, property_address_reported, parcel_number, grantor_name, grantee_name, tokenize='unicode61 remove_diacritics 2')")
        db.execute('INSERT INTO transfer_search SELECT transfer_id, county, municipality, property_address_reported, parcel_number, grantor_name, grantee_name FROM transfers')
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT count(*) FROM transfers').fetchone()[0] == len(cleaned)
        assert db.execute('SELECT count(*) FROM source_records').fetchone()[0] == len(selected)
        assert db.execute('SELECT count(*) FROM transfer_search').fetchone()[0] == len(cleaned)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--baseline', type=Path, default=Path(__file__).with_name('schema_baseline.json'))
    parser.add_argument('--compare', action='store_true', help='Report header differences; do not import')
    parser.add_argument('--output', type=Path, default=Path('parsed_retr'))
    parser.add_argument('--month', type=parse_month, default='2026-09')
    args = parser.parse_args()
    if args.compare:
        header, rows = read_csv(args.source)
        expected = json.loads(args.baseline.read_text())['source_headers']
        print(json.dumps({'file': args.source.name, 'columns': len(header), 'rows': len(rows),
                         'added_headers': [x for x in header if x not in expected],
                         'missing_headers': [x for x in expected if x not in header],
                         'same_header_order': header == expected,
                         'same_header_set': set(header) == set(expected)}, indent=2))
        return
    print(json.dumps(build(args.source, args.output, args.month, args.baseline), indent=2))


if __name__ == '__main__':
    main()
