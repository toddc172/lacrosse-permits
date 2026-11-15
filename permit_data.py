"""Compatibility entry points for versioned parsing, editorial policy and dataset assembly."""
import csv
import hashlib
from permit_common import (ValidationError, clean, parse_date, cents, municipality, base_record,
                           MUNICIPALITIES, MUNICIPALITY_SOURCE, MUNICIPALITY_SOURCE_URL)
from permit_schemas import CSV_FIELDS, PDF_FIELDS, LEGACY_PDF_FIELDS, SUPPORTED_HEADERS
from permit_policy import redact_public_contacts, apply_exclusion_policy, EXCLUDED_PERMIT_TYPES
from permit_validation import validate_unique, PUBLIC_FIELDS
from permit_city import parse_city_csv
from permit_county import (parse_county_pdf, parse_legacy_county, finish_county_records,
                           county_page_tables, remove_clipped_header_wrappers, owner_name)
from permit_dataset import public_dataset

PARSER_VERSION = "1.4.0"
# Historical import retained for older callers; new code uses the accurate name.
omit_formatted_phones = redact_public_contacts

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
    result = apply_exclusion_policy(result)
    return {**result, "sha256": hashlib.sha256(raw).hexdigest(), "parser_version": PARSER_VERSION}

