"""Editorial exclusions and contact redaction applied to parsed records."""
import re
from permit_common import ValidationError, clean

def redact_public_contacts(record):
    """Redact recognizable contact tokens, preserving private originals."""
    phone = r"(?:\(\d{3}\)\s*|\b\d{3}[-.])\d{3}[-.]\d{4}\b"
    changed = False
    for field in ('owner', 'contractor', 'company', 'description'):
        value = record.get(field)
        if isinstance(value, str) and (re.search(phone, value) or re.search(r'[\w.+-]+@[\w.-]+\.\w+', value)):
            replacement = '[telephone omitted]' if field == 'description' else ''
            value = re.sub(phone, replacement, value)
            value = re.sub(r'[\w.+-]+@[\w.-]+\.\w+', '[email omitted]' if field == 'description' else '', value)
            record[field] = clean(value) or None
            changed = True
    if changed:
        record['notes'].append('Recognizable telephone or email contact details in the source text were omitted from public fields.')
    return record

# Exclusions apply after source reconciliation, never before control totals.
EXCLUDED_PERMIT_TYPES = ("marriage",)


def apply_exclusion_policy(result):
    def excluded(record):
        return any(term in (record.get('permit_type') or '').casefold()
                   for term in EXCLUDED_PERMIT_TYPES)
    retained = [record for record in result['records'] if not excluded(record)]
    removed = len(result['records']) - len(retained)
    if removed:
        result['records'] = retained
        result['controls']['marriage_records_excluded'] = removed
        if not retained:
            raise ValidationError('Marriage license documents are excluded; delete this upload from the review list.')
    return result


def separated_contractor_name(value):
    """Use only a single name line from the legacy Contractor column.

    Never apply this to a mixed contact block or the address/phone columns.
    Ambiguous, multiline, numeric and contact-bearing values remain omitted.
    """
    value = (value or '').strip()
    if (not value or len(value) > 160 or '\n' in value or '\r' in value
            or not re.fullmatch(r"[A-Za-z][A-Za-z .&’'\-]*", value)
            or re.search(r"\bP\.?\s*O\.?\s+BOX\b", value, re.I)
            or value.casefold() in {'none', 'unknown', 'na', 'n a', 'not reported'}):
        return None
    return clean(value)
