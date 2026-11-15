"""Parse supported city building CSV reports."""
import csv
import io
import re
from permit_common import ValidationError, clean, base_record, cents, parse_date
from permit_schemas import CSV_FIELDS, canonical_headers
from permit_policy import redact_public_contacts
from permit_validation import validate_unique

def parse_city_csv(raw: bytes, source_name: str):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError("Building CSV must use UTF-8 encoding") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    reader.fieldnames = canonical_headers(reader.fieldnames, "city-v1")
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
        records.append(redact_public_contacts(r))
    if not records:
        raise ValidationError("Building CSV has no permit rows; review before removing records")
    dates = [r["issued_date"] for r in records]
    for r in records:
        r.update(report_start=min(dates), report_end=max(dates))
    validate_unique(records)
    return {"records": records, "controls": {"count": len(records),
            "cost_cents": sum(r["project_cost_cents"] or 0 for r in records),
            "period_basis": "Issue dates in rows; not the filename"}}

