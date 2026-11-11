"""Validated parsers for La Crosse city CSV and county M5 zoning PDF reports.

No AI or OCR inference. Unknown layouts fail without changing the public dataset.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

import pdfplumber
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ContentStream

PARSER_VERSION = "1.2.0"
MUNICIPALITY_SOURCE = "https://www.revenue.wi.gov/Pages/UST/parcels.aspx"
# Wisconsin Department of Revenue, La Crosse County parcel-prefix table.
# Verified Nov. 9, 2026. Municipality is derived from the work parcel, never
# from the owner's mailing address or the contractor's address.
MUNICIPALITIES = {
    1: "Town of Bangor", 2: "Town of Barre", 3: "Town of Burns",
    4: "Town of Campbell", 5: "Town of Farmington", 6: "Town of Greenfield",
    7: "Town of Hamilton", 8: "Town of Holland", 9: "Town of Medary",
    10: "Town of Onalaska", 11: "Town of Shelby", 12: "Town of Washington",
    13: "Village of Bangor", 14: "Village of Holmen", 15: "Village of Rockland",
    16: "Village of West Salem", 17: "City of La Crosse", 18: "City of Onalaska",
}
CSV_FIELDS = [
    "PermitNumber", "PermitType", "PermitWorkClass", "ProjectCost",
    "ProjectDescription", "ParcelNumber", "PermitAddress", "Owner",
    "Contractor", "PermitApplyDate", "PermitIssueDate",
]
PDF_FIELDS = [
    "Department", "Application Date", "Permit Date", "Permit Number",
    "Permit Type", "Permit Status", "Parcel Owner", "Work Printkey / Address",
    "Use Group", "Company", "Contractor", "Work Description", "Fee Total",
    "Cost of Construction", "Sq Footage", "Expiration Date", "Days Inactive",
    "Parcel Zone", "Flood Zone", "Flags",
]
PUBLIC_FIELDS = {
    "id", "jurisdiction", "category", "permit_number", "permit_type",
    "work_class", "issued_date", "application_date", "status", "address",
    "municipality", "municipality_method", "parcel", "owner", "contractor",
    "company", "description", "project_cost_cents", "fee_cents",
    "source_file", "source_pages", "report_start", "report_end", "notes", "department",
}


class ValidationError(ValueError):
    pass


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_date(value, required=False):
    value = clean(value)
    if not value and not required:
        return None
    try:
        return datetime.strptime(value, "%m/%d/%Y").date().isoformat()
    except ValueError as exc:
        raise ValidationError(f"Invalid date: {value!r}") from exc


def cents(value):
    value = clean(value)
    if not value:
        return None
    if not re.fullmatch(r"\$?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{1,2})?", value):
        raise ValidationError(f"Invalid money value: {value!r}")
    try:
        return int(Decimal(value.replace("$", "").replace(",", "")) * 100)
    except InvalidOperation as exc:
        raise ValidationError(f"Invalid money value: {value!r}") from exc


def municipality(parcel):
    match = re.fullmatch(r"(\d{1,3})-\d{5}-\d{1,3}", parcel)
    if not match or int(match[1]) not in MUNICIPALITIES:
        raise ValidationError(f"Unrecognized La Crosse County parcel: {parcel!r}")
    return MUNICIPALITIES[int(match[1])]


def base_record(jurisdiction, number, parcel, source_name, allow_missing_parcel=False):
    if not number:
        raise ValidationError("Missing permit number")
    return {
        "id": f"{jurisdiction}:{number.upper()}", "jurisdiction": jurisdiction,
        "permit_number": number, "parcel": parcel,
        "municipality": municipality(parcel) if parcel or not allow_missing_parcel else "Not reported",
        "municipality_method": "Work parcel prefix" if parcel else "Not reported",
        "source_file": source_name, "source_pages": [], "notes": [],
        "owner": None, "contractor": None, "company": None, "work_class": None,
        "fee_cents": None, "report_start": None, "report_end": None,
    }


def parse_city_csv(raw: bytes, source_name: str):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError("Building CSV must use UTF-8 encoding") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    if reader.fieldnames != CSV_FIELDS:
        raise ValidationError("Building CSV headers changed; review the parser")
    records = []
    for line_number, row in enumerate(reader, 2):
        if None in row or any(value is None for value in row.values()):
            raise ValidationError(f"Malformed CSV record near row {line_number}")
        row = {key: clean(value) for key, value in row.items()}
        if not any(row.values()):
            continue
        r = base_record("city-building", row["PermitNumber"], row["ParcelNumber"], source_name)
        if r["municipality"] != "City of La Crosse":
            raise ValidationError("City building CSV contains a parcel outside La Crosse")
        if not row["PermitType"].startswith("Building - ") or not row["PermitAddress"]:
            raise ValidationError(f"Missing or unexpected building fields for {r['permit_number']}")
        r.update({
            "category": "Building", "permit_type": row["PermitType"],
            "work_class": row["PermitWorkClass"] or None,
            "project_cost_cents": cents(row["ProjectCost"]),
            "description": row["ProjectDescription"], "address": row["PermitAddress"],
            "owner": row["Owner"] or None, "contractor": row["Contractor"] or None,
            "application_date": parse_date(row["PermitApplyDate"]),
            "issued_date": parse_date(row["PermitIssueDate"], required=True),
            # The city CSV contains an issue date, but no current-status column.
            "status": None,
        })
        if not re.match(r"\d", r["address"]):
            r["notes"].append("The source does not supply a street number.")
        records.append(r)
    if not records:
        raise ValidationError("Building CSV has no permit rows; review before removing records")
    dates = [r["issued_date"] for r in records]
    for r in records:
        r.update(report_start=min(dates), report_end=max(dates))
    validate_unique(records)
    return {"records": records, "controls": {"count": len(records),
            "cost_cents": sum(r["project_cost_cents"] or 0 for r in records),
            "period_basis": "Issue dates in rows; not the filename"}}


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
                if [clean(value) for value in row] != PDF_FIELDS:
                    raise ValidationError("County PDF column layout changed")
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
    rows, control, by_type = [], None, None
    with pdfplumber.open(io.BytesIO(prepared)) as doc:
        head = doc.pages[0].extract_text() or ""
        if "La Crosse County" not in head or "M5 New Permit Report" not in head:
            raise ValidationError("PDF is not the supported La Crosse County M5 permit report")
        period = re.search(r"From\s+(\d{1,2}/\d{1,2}/\d{4})\s+To\s+(\d{1,2}/\d{1,2}/\d{4})", head)
        if not period:
            raise ValidationError("County PDF lacks its reporting period")
        start, end = parse_date(period[1], True), parse_date(period[2], True)
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
                        if [clean(v) for v in row] != PDF_FIELDS:
                            raise ValidationError("County PDF column layout changed")
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
                        if clean(row[3]) and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}", clean(row[3])) or not re.search(r"\d", clean(row[3]))):
                            raise ValidationError("Unexpected county permit identifier")
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
    records = []
    for row in rows:
        c = row["cells"]
        department = clean(c[0])
        if department not in ("Zoning Department", "Health Department", "Land Conservation"):
            raise ValidationError("Unexpected county department")
        work = c[7].split("/", 1)
        if len(work) != 2 and clean(c[7]):
            raise ValidationError("Work parcel/address is missing its separator")
        parcel, address = (clean(work[0]), clean(work[1])) if len(work) == 2 else ("", "")
        number = clean(c[3])
        r = base_record("county-zoning", number or "UNREPORTED", parcel, source_name, allow_missing_parcel=True)
        r.update({
            "category": "Zoning", "department": department,
            "permit_type": clean(c[4]), "status": clean(c[5]),
            "application_date": parse_date(c[1]), "issued_date": parse_date(c[2], True),
            "address": address or "Not reported in source", "owner": owner_name(c[6]),
            # Contractor cells mix names, home/business addresses, phones and
            # emails without separators. Publish no guessed contractor name.
            "contractor": None, "company": clean(c[9]) or None,
            "description": clean(c[11]), "fee_cents": cents(c[12]),
            "project_cost_cents": cents(c[13]), "source_pages": row["pages"],
            "report_start": start, "report_end": end,
        })
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
        if not address:
            r['notes'].append('The source does not supply a work street address; no owner mailing address was substituted.')
        if not parcel:
            r['notes'].append('The source does not supply a work parcel; municipality is not inferred.')
        if department != 'Zoning Department':
            r['notes'].append('Source department: ' + department + '. Included in the county M5 report.')
        if not start <= r["issued_date"] <= end:
            raise ValidationError("County issue date is outside the report period")
        if not r["status"] or not r["permit_type"]:
            raise ValidationError("Missing county permit type/status")
        records.append(r)
    validate_unique(records)
    observed = {"count": len(records), "fee_cents": sum(r["fee_cents"] or 0 for r in records),
                "cost_cents": sum(r["project_cost_cents"] or 0 for r in records)}
    if control != observed or dict(Counter(r["permit_type"] for r in records)) != by_type:
        raise ValidationError(f"County PDF reconciliation failed: expected {control}, got {observed}")
    return {"records": records, "controls": {**control, "by_type": by_type,
            "clipped_header_wrappers_removed": removed}}


def validate_unique(records):
    ids = [r["id"] for r in records]
    if len(ids) != len(set(ids)):
        raise ValidationError("Duplicate permit number within one jurisdiction/report")
    for r in records:
        if set(r) - PUBLIC_FIELDS:
            raise ValidationError("Unexpected public field")
        # Do not pass source contact columns or unexpected email/phone material.
        public = json.dumps(r)
        if re.search(r"[\w.+-]+@[\w.-]+\.\w+", public) or re.search(r"(?:\(\d{3}\)\s*|\b\d{3}[-.])\d{3}[-.]\d{4}\b", public):
            raise ValidationError(f"Contact information needs review: {r['permit_number']}")


def parse_report(raw, name):
    try:
        if name.lower().endswith(".csv"):
            result = parse_city_csv(raw, name)
        elif name.lower().endswith(".pdf"):
            result = parse_county_pdf(raw, name)
        else:
            raise ValidationError("Only building CSV and county zoning PDF files are supported")
    except (csv.Error, KeyError, TypeError, IndexError) as exc:
        raise ValidationError(f"Report structure needs review: {type(exc).__name__}") from exc
    return {**result, "sha256": hashlib.sha256(raw).hexdigest(), "parser_version": PARSER_VERSION}


def public_dataset(files, generated_at=None):
    """Latest modified source wins per jurisdiction + permit ID.

    Missing files remain in private state. Moving/removing a source does not
    silently erase public history. A changed file replaces its own rows.
    """
    records = {}
    for _, entry in sorted(files.items(), key=lambda pair: (pair[1]["modified"], pair[0])):
        for r in entry["records"]:
            records[r["id"]] = r
    result = sorted(records.values(), key=lambda r: (r["issued_date"], r["id"]), reverse=True)
    validate_unique(result)
    return {
        "schema_version": 1, "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "latest_issue_date": max((r["issued_date"] for r in result), default=None),
        "record_count": len(result), "source_count": len(files), "records": result,
        "municipality_source": MUNICIPALITY_SOURCE,
        "methodology": "City building CSV and La Crosse County M5 zoning reports. Work locations use the work parcel/address. Municipalities derive from Wisconsin Department of Revenue parcel prefixes. Blank costs remain unreported. Costs describe permits, not distinct projects or completed construction.",
    }
