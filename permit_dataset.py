"""Assemble validated public records; network deployment lives elsewhere."""
from datetime import datetime, timezone
from permit_common import MUNICIPALITY_SOURCE
from permit_validation import validate_unique

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

