"""Shared record primitives and reviewed municipality configuration."""
import json
import re
from pathlib import Path
from datetime import datetime
from decimal import Decimal, InvalidOperation

_municipalities = json.loads((Path(__file__).parent / 'config/municipalities.json').read_text())
if (_municipalities.get('schema_version') != 1 or _municipalities.get('county') != 'La Crosse'
        or not _municipalities.get('source_url', '').startswith('https://www.revenue.wi.gov/')):
    raise ValueError('Invalid municipality configuration')
MUNICIPALITY_SOURCE_URL = _municipalities['source_url']
MUNICIPALITY_SOURCE = MUNICIPALITY_SOURCE_URL  # Compatibility with stored dataset metadata.
MUNICIPALITIES = {int(key): value for key, value in _municipalities['prefixes'].items()}
if not MUNICIPALITIES or any(not isinstance(name, str) or not name for name in MUNICIPALITIES.values()):
    raise ValueError('Invalid municipality names')

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

