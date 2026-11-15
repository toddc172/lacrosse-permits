"""Versioned, explicit report column definitions; no fuzzy matching."""
import json
from pathlib import Path
from permit_common import ValidationError, clean

SCHEMAS = json.loads((Path(__file__).parent / 'config/report-schemas.json').read_text())
if SCHEMAS.get('schema_version') != 1:
    raise ValueError('Unsupported report schema configuration')
SUPPORTED_HEADERS = SCHEMAS['schemas']
CSV_FIELDS = SUPPORTED_HEADERS['city-v1']['fields']
PDF_FIELDS = SUPPORTED_HEADERS['county-modern-v1']['fields']
LEGACY_PDF_FIELDS = SUPPORTED_HEADERS['county-legacy-v1']['fields']


def canonical_headers(headers, schema_name):
    schema = SUPPORTED_HEADERS[schema_name]
    aliases = {name: name for name in schema['fields']}
    for canonical, variations in schema['aliases'].items():
        for variant in variations:
            if variant in aliases and aliases[variant] != canonical:
                raise ValueError('Ambiguous configured header alias')
            aliases[variant] = canonical
    actual = [aliases.get(clean(header)) for header in (headers or [])]
    if actual != schema['fields']:
        raise ValidationError('Report column layout changed; unsupported or duplicate header')
    return actual
