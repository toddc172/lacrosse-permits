"""County M5 identification, extraction and conversion with mandatory reconciliation."""
import hashlib
import io
import json
import re
from datetime import date
import pdfplumber
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ContentStream
from permit_common import ValidationError, clean, cents, parse_date, base_record
from permit_schemas import PDF_FIELDS, LEGACY_PDF_FIELDS, canonical_headers
from permit_policy import redact_public_contacts, separated_contractor_name
from permit_validation import reconcile_county, validate_county_identifier

def identify_county_report(doc):
    head = doc.pages[0].extract_text() or ""
    if "La Crosse County" not in head or not any(title in head for title in ("M5 New Permit Report", "M5 Permit Report")):
        raise ValidationError("PDF is not the supported La Crosse County M5 permit report")
    period = re.search(r"From\s+(\d{1,2}/\d{1,2}/\d{4})\s+To\s+(\d{1,2}/\d{1,2}/\d{4})", head)
    if not period:
        raise ValidationError("County PDF lacks its reporting period")
    start, end = parse_date(period[1], True), parse_date(period[2], True)
    return start, end, "M5 New Permit Report" not in head


def remove_clipped_header_wrappers(raw):
    """Discard M5 header-only Form wrappers that re-draw the entire first page.

    M5 embeds a nested copy of the first page's table under a header-sized
    clipping rectangle. Ordinary PDF extraction wrongly reads its invisible
    body rows. Remove only wrappers with exactly q/cm/Do/Q, a single nested
    Form, and a short, offset bounding box. The primary tables stay intact.
    This transforms an in-memory extraction copy; the source stays unchanged.
    Count/type/cost/fee checks below still gate publication.
    """
    reader = PdfReader(io.BytesIO(raw))
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    removed = 0
    for page in writer.pages:
        for value in page["/Resources"].get("/XObject", {}).values():
            obj = value.get_object()
            box = obj.get("/BBox")
            if obj.get("/Subtype") != "/Form" or not box:
                continue
            ops = ContentStream(obj, writer).operations
            if (0 < float(box[3]) - float(box[1]) < 40
                    and float(box[1]) > float(page.mediabox.height) / 2
                    and [op for _, op in ops] == [b"q", b"cm", b"Do", b"Q"]):
                target = obj["/Resources"]["/XObject"][ops[2][0][0]].get_object()
                target_box = target.get("/BBox")
                if target.get("/Subtype") == "/Form" and target_box and float(target_box[3]) - float(target_box[1]) > 100:
                    obj.set_data(b"")
                    removed += 1
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), removed

def owner_name(cell):
    # County owner cell also includes a mailing address and sometimes a phone.
    # Keep only the name preceding the first address/number line.
    lines = [clean(line) for line in cell.splitlines() if clean(line)]
    name = []
    for line in lines:
        if re.match(r"(?:\d|[NSEW]\d|P\.?\s*O\.?\s+BOX)", line, re.I):
            break
        name.append(line)
    return clean(" ".join(name)) or None

def county_page_tables(page, columns):
    """Repair missing internal borders using this report's verified header.

    Some M5 rows have one spanning cell where the vertical border segments
    are missing. Reuse the 20 column boundaries from the exact supported
    header, within the original table bounds. Summary tables are untouched.
    No text or source values are inferred; reconciliation remains mandatory.
    """
    extracted = []
    for found in page.find_tables():
        table = found.extract()
        if not table or len(table[0]) != 20:
            extracted.append(table)
            continue
        for index, row in enumerate(table):
            if clean(row[0]) == "Department" and clean(row[1]) == "Application Date":
                canonical_headers(row, "county-modern-v1")
                boxes = found.rows[index].cells
                if any(box is None for box in boxes):
                    raise ValidationError("County header column boundaries are incomplete")
                columns = sorted({x for box in boxes for x in (box[0], box[2])})
                if len(columns) != 21:
                    raise ValidationError("County header must define 20 columns")
        if any(value is None for row in table for value in row):
            if (columns is None or abs(found.bbox[0] - columns[0]) > 2
                    or abs(found.bbox[2] - columns[-1]) > 2):
                raise ValidationError("County table cannot be aligned with its verified header")
            repaired = page.crop(found.bbox).extract_tables({
                "vertical_strategy": "explicit", "explicit_vertical_lines": columns})
            if len(repaired) != 1 or any(len(row) != 20 for row in repaired[0]):
                raise ValidationError("County table border repair needs review")
            table = repaired[0]
        extracted.append(table)
    return extracted, columns

def parse_county_pdf(raw: bytes, source_name: str):
    prepared, removed = remove_clipped_header_wrappers(raw)
    with pdfplumber.open(io.BytesIO(prepared)) as doc:
        start, end, legacy = identify_county_report(doc)
        if legacy:
            return parse_legacy_county(doc, start, end, source_name, removed)
        rows, control, by_type = extract_modern_county(doc)
    return finish_county_records(rows, control, by_type, start, end, source_name, removed)


def extract_modern_county(doc):
    rows, control, by_type = [], None, None
    saw_header, columns = False, None
    for page_number, page in enumerate(doc.pages, 1):
        tables, columns = county_page_tables(page, columns)
        for table in tables:
            if not table:
                continue
            if len(table[0]) == 5 and clean(table[0][0]).lower() == "count by type":
                by_type = {clean(row[0]): int(clean(row[1])) for row in table[2:]
                           if row[0] and clean(row[0]) != "Total:"}
            if len(table[0]) != 20:
                continue
            for row in table:
                row = [value or "" for value in row]
                if clean(row[0]) == "Department" and clean(row[1]) == "Application Date":
                    canonical_headers(row, "county-modern-v1")
                    saw_header = True
                    continue
                if clean(row[0]) == "Total:":
                    if control is not None:
                        raise ValidationError("Multiple county detail totals")
                    control = {"count": int(clean(row[1])), "fee_cents": cents(row[12]),
                               "cost_cents": cents(row[13])}
                    continue
                if not any(clean(v) for v in row):
                    continue
                if clean(row[3]) or (clean(row[0]) in ("Zoning Department", "Zoning", "Health Department", "Health", "Land Conservation") and clean(row[2])):
                    # Preserve the source's identifier, including AGC agricultural
                    # certificates and other year-number series in this county
                    # report. Department, type summaries and totals still gate it.
                    validate_county_identifier(clean(row[3]))
                    rows.append({"cells": row, "pages": [page_number]})
                else:
                    if not rows or (clean(row[0]) not in ("", "Department")):
                        raise ValidationError("Unrecognized county continuation row")
                    for i, value in enumerate(row):
                        if value:
                            rows[-1]["cells"][i] += "\n" + value
                    rows[-1]["pages"].append(page_number)
    if not saw_header or control is None or by_type is None:
        raise ValidationError("County PDF is missing its column header or control summaries")
    return rows, control, by_type

def finish_county_records(rows, control, by_type, start, end, source_name, removed, legacy=False):
    records = [county_record(row, start, end, source_name, legacy) for row in rows]
    reconcile_county(records, control, by_type)
    return {"records": records, "controls": {**control, "by_type": by_type,
            "clipped_header_wrappers_removed": removed}}


def county_record(row, start, end, source_name, legacy=False):
    c = row["cells"]
    department = clean(c[0])
    if department not in ("", "Zoning Department", "Health Department", "Land Conservation"):
        raise ValidationError("Unexpected county department")
    work = c[7].split("/", 1)
    if len(work) != 2 and clean(c[7]):
        raise ValidationError("Work parcel/address is missing its separator")
    parcel, address = (clean(work[0]), clean(work[1])) if len(work) == 2 else ("", "")
    number = clean(c[3])
    r = base_record("county-zoning", number or "UNREPORTED", parcel, source_name, allow_missing_parcel=True)
    r.update({
        "category": "Zoning", "department": department or None,
        "permit_type": clean(c[4]), "status": clean(c[5]) or None,
        "application_date": parse_date(c[1]), "issued_date": parse_date(c[2], True),
        "address": address or "Not reported in source", "owner": owner_name(c[6]),
        # Only legacy reports have a separately labeled name column.
        # Modern mixed contact blocks remain suppressed.
        "contractor": separated_contractor_name(row.get("contractor_name", "")) if legacy else None,
        "company": clean(c[9]) or None,
        "description": clean(c[11]), "fee_cents": cents(c[12]),
        "project_cost_cents": cents(c[13]), "source_pages": row["pages"],
        "report_start": start, "report_end": end,
    })
    assign_county_identity(r, number, parcel, address)
    if not address:
        r['notes'].append('The source does not supply a work street address; no owner mailing address was substituted.')
    if not parcel:
        r['notes'].append('The source does not supply a work parcel; municipality is not inferred.')
    if not department:
        r['notes'].append('The source does not supply a department for this record.')
    elif department != 'Zoning Department':
        r['notes'].append('Source department: ' + department + '. Included in the county M5 report.')
    if not date.fromisoformat(start) <= date.fromisoformat(r["issued_date"]) <= date.fromisoformat(end):
        raise ValidationError("County issue date is outside the report period")
    if (not r["status"] and not legacy) or not r["permit_type"]:
        raise ValidationError("Missing county permit type/status")
    if legacy:
        r["notes"].append("Legacy report: legal address is linked to the work parcel; per-permit status is not supplied.")
    return redact_public_contacts(r)


def parse_legacy_county(doc, start, end, source_name, removed):
    rows, control, by_type, columns = [], None, None, None
    for page_number, page in enumerate(doc.pages, 1):
        for found in page.find_tables():
            table = found.extract()
            if not table:
                continue
            if len(table[0]) == 4 and clean(table[0][0]).lower() == 'count by type':
                by_type = {clean(row[0]): int(clean(row[1])) for row in table[2:] if clean(row[0]) and clean(row[0]) != 'Total:'}
                continue
            if len(table[0]) != 21:
                continue
            table, columns = prepare_legacy_table(page, found, table, columns)
            for row in table:
                row = [v or '' for v in row]
                if clean(row[0]) == 'Application Date':
                    continue
                if clean(row[0]) == 'Total:':
                    if control is not None:
                        raise ValidationError('Multiple legacy county detail totals')
                    control = {'count': int(clean(row[2])), 'cost_cents': cents(row[16]), 'fee_cents': cents(row[17])}
                    continue
                if not any(clean(v) for v in row):
                    continue
                if clean(row[1]) or clean(row[2]):
                    normalized = ['', row[0], row[1], row[2], row[4], '', row[5], row[7] + ' / ' + row[6], row[9], row[10], '', row[14], row[17], row[16], row[15], '', '', row[18], row[19], row[20]]
                    rows.append({'cells': normalized, 'pages': [page_number], 'contractor_name': row[11]})
                else:
                    if not rows:
                        raise ValidationError('Unrecognized legacy continuation row')
                    mapping = {0:1, 4:4, 5:6, 6:7, 7:7, 9:8, 10:9, 14:11, 15:14, 16:13, 17:12, 18:17, 19:18, 20:19}
                    for old, new in mapping.items():
                        if row[old]:
                            rows[-1]['cells'][new] += '\n' + row[old]
                    if row[11]:
                        rows[-1]['contractor_name'] += '\n' + row[11]
                    rows[-1]['pages'].append(page_number)
    if columns is None or control is None or by_type is None:
        raise ValidationError('Legacy county header or control summaries missing')
    return finish_county_records(rows, control, by_type, start, end, source_name, removed, legacy=True)



def prepare_legacy_table(page, found, table, columns):
    for index, row in enumerate(table):
        if clean(row[0]) == 'Application Date':
            canonical_headers(row, 'county-legacy-v1')
            cells = found.rows[index].cells
            if any(cell is None for cell in cells):
                raise ValidationError('Legacy header column boundaries incomplete')
            columns = sorted({x for cell in cells for x in (cell[0], cell[2])})
            if len(columns) != 22:
                raise ValidationError('Legacy report must have 21 columns')
    if columns is None:
        raise ValidationError('Legacy county header missing')
    if any(value is None for row in table for value in row):
        fixed = page.crop(found.bbox).extract_tables({'vertical_strategy': 'explicit', 'explicit_vertical_lines': columns})
        if len(fixed) != 1 or any(len(row) != 21 for row in fixed[0]):
            raise ValidationError('Legacy county border repair needs review')
        table = fixed[0]
    return table, columns


def assign_county_identity(r, number, parcel, address):
    if not number:
        identity = [r['permit_type'], r['application_date'], r['issued_date'], parcel, address]
        r['id'] = 'county-zoning:unreported:' + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
        r['permit_number'] = None
        r['notes'].append('The source does not supply a permit number. Cross-report matching uses reported type, dates and work parcel/address.')
    elif number.isdigit():
        r['id'] = 'county-zoning:' + r['permit_type'] + ':' + r['issued_date'][:4] + ':' + number
    else:
        # Older reports vary separators in ACUP, CUP, ZPT and VAR series.
        canonical = re.sub(r'[ ._-]+', '', number.upper())
        r['id'] = 'county-zoning:' + canonical if re.match(r'^(ACUP|CUP|ZPT|VAR)', canonical) else r['id']
