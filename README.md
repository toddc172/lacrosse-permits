# La Crosse building and zoning permits

Prepared for the La Crosse Tribune. Updated Nov. 10, 2026.

The newsroom upload page and public Pages reader are deployed. Todd confirmed a successful GitHub publishing run, **41 permits on the public site**, and Published status for both original reports. The BLOX preview loads the embedded reader. Parser **1.2.0** now reconciles all 17 newly attached backfill PDFs, the prior September PDF and the original reports locally. It handles older identifiers, missing source fields and split rows; the reader identifies source departments in county reports. The updated package still needs deployment and a successful live backfill run. Follow `PUBLISHING.md`. Newsroom staff use the shared upload bookmark; they do not need GitHub or Cloudflare accounts for routine updates.

## Current design

| Function | Service |
| --- | --- |
| Password-protected shared upload page | Existing Cloudflare Worker, Workers Free |
| Private original reports and import state | Existing Workers KV namespace, KV Free |
| Daily import at 7:17 a.m. Central | Standard GitHub-hosted runner in a public repository |
| Public search/filter/export database | Static Cloudflare Pages Free site |
| Tribune website | BLOX iframe pointing to the public Pages URL |

The budget is **$0 in new service charges**. Keep the products on their Free plans, use the provider subdomain and standard public-repository runner, and stop if a free limit is reached. No R2, Pages Functions, paid AI, purchased domain, workflow artifacts or paid cache storage are used. Original files and private contact blocks stay outside the public repository and site.

The older Azure/OneDrive adapters are retained only as reference in the full implementation package. They do not satisfy the current budget and are not in the separate GitHub setup package. Do not deploy them for this plan. `IMPLEMENTATION.md` describes that older route.

## Data and reader behavior

The test sources produce **41 permits**: 29 city building records with issue dates Oct. 26-30, 2026, and 12 county zoning records for the report period Oct. 22-28. The city filename date is not treated as an issue date. County controls reconcile to $131,159.58 in reported construction costs and $2,850 in fees. These are permit totals, not estimates of distinct projects.

Readers can search work addresses, owners, building contractors, descriptions, parcel numbers and permit numbers. They can filter, sort, open details and download matching CSV rows. Work locations come from the work parcel/address rather than the owner's mailing address. Municipalities derive from Wisconsin's parcel-prefix table. Blank costs remain distinct from an explicit $0. Current status is not inferred from an issue date. County contractor contact blocks are omitted.

Validated upload versions are deduplicated by issuing authority and permit number. Newer reports update matching permits while historical records remain. Omitting a row does not automatically retract it. If a changed report fails validation, the whole changed batch is held and the previous public data stays available. See `PUBLISHING.md` for correction, failure and retry details.

The reader fetches saved JSON; it does not rescan uploads. It refreshes once a minute while visible, preserves filters, retains loaded data during a failed fetch and displays separate dataset-update and successful-check timestamps. The starter site explicitly says the first import is pending.

## Files

| File | Purpose |
| --- | --- |
| `PUBLISHING.md` | Current free-account setup and newsroom operation |
| `.github/workflows/daily-permits.yml` | Daily schedule, free-runner guard and publication |
| `publish_permits.py`, `cloudflare_client.py` | Private KV import, static deployment and verification |
| `permit_data.py` | Validated city CSV and county M5 PDF parsers |
| `requirements-publisher.txt` | Two Python parser dependencies |
| `web/index.html` | Public reader interface |
| `cloudflare/` | Working private upload Worker and tests; full package only |
| `blox-embed.html` | BLOX template; replace the placeholder with the actual Pages URL |
| `sample/`, `build_preview.py` | Fixed test preview; full package only |
| `sync_worker.py`, `function_app.py`, `IMPLEMENTATION.md` | Legacy Azure reference; full package only |

Run `python -m unittest discover -s tests -p test_publisher.py -v` to check the daily publisher without credentials. In the full package, `node tests/test_ui.mjs` checks the DOM model and `node cloudflare/test_worker.mjs` checks uploads. Original-report tests skip if the originals are absent from a sibling `source` folder. No real password or token appears in the code. Real scheduling, publication, browser layout and BLOX acceptance remain to be verified during setup.
