# La Crosse Tribune property transfers: production D1

This directory contains the active property transfers system. Pilot migration
finished with 21 reconciled source months and 17,309 transfers; subsequent
backfills add to that baseline. Do not treat that historical total as today's
required record count.

## Active components

| Purpose | Source or service |
| --- | --- |
| Staff uploader | `cloudflare/worker.mjs` and `cloudflare/upload-page.mjs` |
| Single-file uploader for Cloudflare Edit code | `cloudflare/worker.js` |
| Original compressed statewide CSVs and upload status | Existing private transfers KV namespace |
| Validated six-county records and accepted-month ledger | Existing D1 database |
| Source metadata and staff-status contract | `upload_contract.py` |
| CSV parser and public field validation | `transfer_data.py`, `parse_retr.py`, `schema_baseline.json` |
| Production importer | `d1/sync_d1.py` |
| Public search API | `d1/search-worker.mjs` |
| Public reader HTML source | `d1/web/index.html` |
| Generated reader module and standalone search Worker | `d1/reader.mjs`, `d1/dashboard-worker.js` |
| Only active transfer workflow | `../.github/workflows/property-transfers-d1.yml` |

Keep the modular and single-file Worker variants together. They support
different deployment methods and are not duplicate historical revisions.
The `cloudflare/wrangler.toml` and `d1/wrangler.toml` files contain placeholder
resource IDs. Dashboard deployments keep their existing bindings; do not deploy
those placeholders as real configuration.

Current addresses:

- Staff uploader: https://lacrosse-property-transfers-upload.rivervalleydata.workers.dev/
- Public reader/API: https://lacrosse-property-transfers-search.rivervalleydata.workers.dev/

BLOX embeds the public reader. The older transfer Pages project is outside this
code cleanup. Check every embed and bookmark before retiring that hosted project.
No Cloudflare resource is deleted by installing these repository changes.

## Routine operation

1. Staff select the original DOR monthly CSV, review its counts and approve it.
2. The production workflow processes up to three new source months per run,
   subject to the existing daily application safeguards.
3. Source hashes and record fingerprints are checked before each month activates.
   Staged records stay private; accepted months and the public summary activate
   together. Repeated accepted source IDs do not add duplicate records.
4. A rejected source stops its processing run. The uploader names the rejected
   file and links to the GitHub Actions import log. Resolve the source/parser or
   conflict before further importing. There is no automatic correction or
   replacement approval screen. Files may still be uploaded and queued.
5. Refresh status after a run. Staff deletion remains disabled on the server.
   Original CSVs remain in KV, including rejected sources.

The schedule remains `37 4 * * *`, `America/Chicago`. Scheduled execution has
been observed later in the day. The accepted operational requirement is daily
completion, not an exact start time. Check Actions for successful daily runs.
Manual recovery is Actions > Property transfers D1 > Run workflow > main.
There is now no pilot/production selector and no Initialize checkbox.

The workflow requires:

- Variables: `CLOUDFLARE_ACCOUNT_ID`, `TRANSFERS_KV_NAMESPACE_ID`,
  `TRANSFERS_D1_DATABASE_ID`, `TRANSFERS_D1_ENABLED=true`.
- Secrets: existing `CLOUDFLARE_API_TOKEN` for KV and
  `TRANSFERS_D1_API_TOKEN` for D1.

`TRANSFERS_D1_MIGRATE_ENABLED` is no longer read and can be removed after this
cleanup is installed. `TRANSFERS_D1_API_URL` and `TRANSFERS_PAGES_PROJECT` are not
needed by the production importer, but may remain as reference until all external
references and fallback needs have been reviewed. Do not remove permits variables
or secrets: permits and transfers share the account and some credentials.

## Safeguards and limits

| Safeguard | Current setting and reason |
| --- | --- |
| Compressed source | 8 MiB; existing upload and source-download bound |
| Uncompressed source | 64 MiB; parser bound against excessive decompression |
| Monthly work per scheduled/manual run | Three new activated months; preserves current behavior |
| Application daily usage budget | 35,000 rows written / 1,000,000 read; conservative importer safeguards, not provider quota definitions |
| Row insert request | 20 statements, 12 bound parameters per statement; existing request batching |
| Upload settling | 90 seconds before processing a newly approved file |
| Coverage report | Missing source months since January 2021 through latest accepted recorded month |

Usage is recorded under `d1:usage:YYYY-MM-DD` using UTC dates. This ledger is
separate from provider-wide usage; the safeguards do not guarantee account-wide
quota headroom. Index and full-text writes contribute to import work. At the
budget pause, leave staged rows and originals intact and resume after the next
UTC day. A green workflow may report a normal budget pause rather than a newly
activated month.

Legacy 256 MiB static archive bounds and 512 KiB public JSON chunks no longer
apply to this production search path. Daily search requests return paged results,
not the whole archive. Existing source/parser, API input, cursor and paging limits
remain in force. No paid plan, new service or larger budget is introduced.

## Maintenance and verification

From this directory:

```sh
python -m unittest discover -s tests -p 'test_*.py' -q
python -m unittest discover -s d1/tests -p test_d1.py -q
node tests/test_worker.mjs
node d1/tests/test_worker.mjs
```

After reader edits, run `python d1/build_worker.py` to regenerate both reader
artifacts; then run the tests before any manual search Worker deployment.
The schema and public search Worker were not changed by the cleanup.
For deliberate database recovery only, the importer still accepts `--init` to
apply the existing non-dropping schema. Routine runs must not use it.
`--mode production` remains accepted for compatibility; production is the default.
The optional `--until-budget` batching flag remains available but is not enabled
by this workflow. Pilot mode, pilot audit and migration completion checks are
retired. Never re-enable the old static import workflow alongside production D1.

The separate permits system is outside this directory and remains unchanged.
Its Worker source was not in the supplied repository ZIP. Preserve the deployed
permit uploader headline in future updates:
**Newsroom building and zoning permit uploads**.

## Retained private history

This cleanup does not read, erase or rewrite old `state:current`, `state:chunk:*`
or `d1:migration:complete` KV entries. Production no longer depends on them.
Do not delete them until a separate inventory and backup verifies what is safe.
Retain `uploads/*` originals, `removed/*` markers, `control:status` and the daily
D1 usage ledger. Historical removal markers still prevent previously removed
sources from silently reappearing; they are not migration clutter.
Do not drop D1 tables, indexes, FTS tables, batches or settings.
