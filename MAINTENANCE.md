# Permit parser maintenance

## Required permits uploader headline

The permits staff upload site must use this exact visible headline:

**Newsroom building and zoning permit uploads**

The former wording, `Newsroom report uploads`, is retired. Preserve the approved
wording in the uploader HTML source, any page-title wording that repeats it, and
all generated or standalone Worker bundles. Rebuilding or deploying must not
restore the retired headline. Check the final deployed page after each update.
This requirement applies to the permits uploader, not the property transfers
uploader or either public database heading.

The Nov. 24, 2026 PAGE_HTML attachment is archived as
`cloudflare/permit-upload-page.html`. It already contains the correct headline
and browser title. `cloudflare/build_permit_upload_page.py` validates that wording
and generates `cloudflare/permit-upload-page.constant.js`. Run it with `--check`
to verify the saved artifact, or `--check-worker PATH` to check that a candidate
replacement embeds the maintained template. Rebuild all upstream templates that
supply PAGE_HTML from this source; older external packages are not authoritative.

Only the PAGE_HTML constant was supplied. The full permits Worker request
handler, backend and any external generator remain unavailable. Do not deploy
the page constant alone as a Worker. Keep the working Cloudflare backend until
its complete current source is saved and reviewed.

## Module boundaries

- `permit_data.py`: stable import entry points and parser version.
- `permit_city.py`, `permit_county.py`: source identification, extraction and conversion. County metadata, modern tables, legacy geometry, record conversion and identity assignment have separate functions.
- `permit_common.py`: record primitives, monetary/date parsing and municipality lookup.
- `permit_schemas.py`: exact schema matching using reviewed aliases.
- `permit_policy.py`: explicit marriage exclusion and public-contact redaction; conservative separated-name recovery.
- `permit_validation.py`: public field contract, duplicate identities, contact checks, identifier format and county reconciliation.
- `permit_dataset.py`: deterministic merge into a public dataset. Network publication remains in `publish_permits.py`.

## Reviewed configurations

`config/municipalities.json` retains the existing mapping, DOR source URL and original review date. Check that source before editing the prefix/name map; update the date only after review. It is intentionally a pinned snapshot rather than an unreviewed live feed.

`config/report-schemas.json` names city-v1, county-modern-v1 and county-legacy-v1. Alias additions must be unambiguous and preserve exact column order. PDF header aliases do not authorize changed table geometry. Build the Worker again after changing city aliases. Test both upload acceptance and full Python parsing; neither layer uses fuzzy matching.

## Editorial and data rules

Reconcile complete source counts, fees, costs and type summaries before applying EXCLUDED_PERMIT_TYPES. Marriage documents remain excluded. Redact contact tokens in approved public text fields; the final validation guard rejects unexpected contact material. Never infer a contractor name from a mixed block. Only the separately labeled legacy name column is eligible, with ambiguous values omitted. Record identities and correction precedence remain stable. Unknown identifier formats continue to require review.

Dates are validated as dates at ingestion and serialized to ISO dates in the record contract; county period checks compare date objects. The public JSON schema remains version 1. Bump PARSER_VERSION when accepted archived sources must be reparsed.

## Verification

Run python -m unittest discover -s tests -p test_publisher.py -q. Full source workspaces can also run the complete Python suite, build the Worker, run node cloudflare/test_worker.mjs, and run node tests/test_ui.mjs. Private input reports stay outside the public repository. Install all parser modules/configs in one GitHub commit. Use a fresh workflow run on main to pick up that commit. No paid services or new cloud resources are required.
