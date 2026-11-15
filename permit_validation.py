"""Public-field, identity and source-total validation."""
import json
import re
from collections import Counter
from permit_common import ValidationError

PUBLIC_FIELDS = {
    'address',
    'application_date',
    'category',
    'company',
    'contractor',
    'department',
    'description',
    'fee_cents',
    'id',
    'issued_date',
    'jurisdiction',
    'municipality',
    'municipality_method',
    'notes',
    'owner',
    'parcel',
    'permit_number',
    'permit_type',
    'project_cost_cents',
    'report_end',
    'report_start',
    'source_file',
    'source_pages',
    'status',
    'work_class',
}


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

def reconcile_county(records, control, by_type):
    validate_unique(records)
    observed = {"count": len(records), "fee_cents": sum(r["fee_cents"] or 0 for r in records),
                "cost_cents": sum(r["project_cost_cents"] or 0 for r in records)}
    if control != observed or dict(Counter(r["permit_type"] for r in records)) != by_type:
        raise ValidationError(f"County PDF reconciliation failed: expected {control}, got {observed}")


COUNTY_PERMIT_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}")


def validate_county_identifier(number):
    """Keep the observed modern M5 identifier contract; unknown formats need review."""
    if number and (not COUNTY_PERMIT_IDENTIFIER.fullmatch(number) or not re.search(r"\d", number)):
        raise ValidationError("Unexpected county permit identifier")
