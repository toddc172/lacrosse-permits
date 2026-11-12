# Daily publishing: free Cloudflare + public GitHub

Todd confirmed a successful complete GitHub publishing run, both original reports marked Published, 41 permits on the public Pages reader and a working BLOX embed preview. GitHub holds the API token as a secret; it never needs the newsroom upload password. The embed now contains only the iframe, without a separate-window database link. Its hosting URL remains publicly accessible.

The subsequent backfill exposed two parser limitations. Parser 1.1.0 accepts structured year-number county identifiers such as AGC agricultural certificates while preserving the reported permit type, and repairs missing internal row borders using the exact verified 20-column header and the original table bounds. Counts, types, construction costs and fees must still reconcile. The supplied Sept. 17-23 report now reconciles to 10 permits, $431,500 in reported construction costs and $625 in fees. The original October reports still pass. Deployment of this parser update and the unseen backfill reports remain to be verified.

### Batch update: parser 1.2.0

All 17 additional PDFs supplied for the backfill now reconcile locally, including their counts, type summaries, fees and reported construction costs. The parser preserves legacy identifiers such as ACUP, ZPT, VAR and agricultural waivers. Separator variants in ACUP/CUP/ZPT/VAR identifiers share an internal matching key while the source number remains unchanged. Numeric-only source numbers are scoped by permit type and issue year, so a sign permit numbered 4 and a short-term rental numbered 4 remain separate.

Missing source numbers stay explicitly unreported, with an internal matching hash based on reported type, dates and work location. Blank work addresses are displayed as unreported; owner mailing addresses are never substituted. Missing work parcels leave municipality unreported. These are source limitations, not inferred corrections.

Source departments are retained explicitly, including Health Department and Land Conservation entries contained in the county M5 reports. The reader labels this category County reports and shows/exports the source department. The stored category key remains Zoning for compatibility with saved filters. Validated continuation fragments are joined before department/date/location checks. Unknown departments and unreconciled totals still stop publication. Staff review messages now describe fixed problem categories privately without copying raw exception text into public logs.

The batch repair ZIP contains two Python files plus `web/` and `tests/` folders. Upload all four items into the existing GitHub repository root, retaining the supplied paths. No raw PDF, CSV, credential or private snapshot is included. Start a new workflow run on main after committing. Live processing of the complete Cloudflare archive remains to be confirmed.

## Keep this setup free

Use GitHub Free with a **public** repository and the supplied standard `ubuntu-latest` runner. Use the existing Cloudflare **Workers Free / KV Free** account and a **static Cloudflare Pages Free** project. Use its free `pages.dev` address. No card, purchased domain, Pages Functions, R2, paid AI, artifact uploads, or workflow cache storage is required by this code. Do not accept an upgrade prompt to make a limit pass. GitHub evaluates the public-repository guard before starting a runner.

The repository and final permit JSON are public. Original CSV/PDF reports, owner mailing addresses, source contact blocks, API tokens and private import state stay out of the repository and public site. The upload password and the Cloudflare account password stay separate from the API token.

## Setup order

1. Create a public GitHub repository named `lacrosse-permits`, with a README. No paid GitHub features are needed. Give repository write access only to people who should be able to change the processing code and use the publishing token. Newsroom upload users do not need repository access.
2. Upload the **contents** of the package's `github-files` folder to the repository root, **except `.github` for now**. Do not upload the outer ZIP, original reports, `cloudflare-site.zip`, or other folders. Keep the supplied `.gitignore`. Add the workflow last, after the following settings are complete, by creating `.github/workflows/daily-permits.yml` through **Add file → Create new file** and pasting the exact text from `daily-permits-workflow.txt` in this setup package.
3. In Cloudflare, go to **Compute → Workers & Pages → Create application**. Choose **Continue to Pages** if the screen starts with Worker options. In Pages choose **Upload assets** / direct upload, name the project `lacrosse-tribune-permits` (or another available name), and upload **only `cloudflare-site.zip`**. Deploy it. The starter says it is awaiting the first import; it contains no test records. Copy the actual `pages.dev` address shown by Cloudflare. A direct-upload Pages project is required; do not connect a second automatic Git build.
4. In Cloudflare's account overview, copy the account ID. Copy the namespace ID for `lacrosse-permits-files` from **Storage & databases → Workers KV**. These are identifiers, not the upload password.
5. Create a custom Cloudflare API token scoped to **this account only**, with account permissions **Workers KV Storage → Edit** and **Cloudflare Pages → Edit** (some interfaces label write permissions **Write** or **Pages**). Do not use a Global API key. The token needs read/write access to private KV and deployment access to Pages; no DNS, billing or plan permissions are required. These are account-level permissions and are not restricted to just one namespace. Treat the token as a publishing credential and keep it out of screenshots and chat.
6. In the GitHub repository, open **Settings → Secrets and variables → Actions**. Add the following repository settings. Use **Variables** for the three identifiers, and **Secrets** for the token.

| Kind | Exact name | Value |
| --- | --- | --- |
| Variable | `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID, 32 hexadecimal characters |
| Variable | `CLOUDFLARE_KV_NAMESPACE_ID` | ID of the existing `lacrosse-permits-files` namespace |
| Variable | `CLOUDFLARE_PAGES_PROJECT` | Exact Pages project name; no URL or `.pages.dev` suffix |
| Secret | `CLOUDFLARE_API_TOKEN` | The custom account-scoped API token |

7. If repository workflow permissions require it, allow **Read and write permissions** under **Settings → Actions → General → Workflow permissions**. The workflow requests `contents: write` solely to save the already-public, validated JSON and its daily check time. A branch rule blocking this bot's commits requires an approved repository configuration; the code does not bypass branch protection.
8. Add `.github/workflows/daily-permits.yml` on the default branch. In **Actions → Daily permit import → Run workflow**, select that branch and run it. The first successful run should publish **41 permits: 29 building and 12 zoning**, assuming the two original reports are still the only uploads. The county controls are $131,159.58 in reported construction costs and $2,850 in fees. The upload page should then show Published instead of Stored.
9. Open the actual Pages address. Check the work locations, filters, details, exports and a narrow mobile window. Add that URL to the BLOX iframe template and verify it on a Tribune test page. The static site's CSP permits framing by `lacrossetribune.com` and its subdomains. An unrelated BLOX staging origin must be added deliberately before testing that origin.

Do not share passwords or the API token in chat. Account and namespace IDs, project names, workflow success/failure codes and the public Pages URL can be used for setup troubleshooting.

## Daily behavior

- The schedule is **7:17 a.m. America/Chicago**, including daylight saving changes. GitHub may delay or occasionally drop a scheduled run. **Run workflow** provides a manual retry. This workflow does the actual import.
- One production workflow is the only writer. Concurrency queues overlapping runs and never cancels one halfway through publication. Do not enable another repository's scheduler against the same namespace/project. KV is eventually consistent and supplies no transactional lock or compare-and-swap guarantee.
- The job lists `uploads/` using cursor pagination, verifies each metadata identity and downloaded byte hash, parses only new sources (or reparses after a parser-version change), and holds the whole changed batch if a report fails validation. Sources less than 90 seconds old hold the batch for a later retry. A source not yet visible globally may wait until the next daily scan.
- The job deploys exactly `index.html`, curated `permits.json` and `_headers`. It verifies that the production URL serves the exact new JSON before saving `state:current` and then `control:status` in private KV. No raw report is written to the runner's disk. A failed import or deployment preserves the previous public data. If deployment succeeds but verification or a later private state write fails, the new site may already be live; the job does not acknowledge unverified publication and can safely retry.
- The revised public-site check compares all decoded JSON data, allowing insignificant whitespace/key-order differences, and supports bounded gzip/deflate decoding. It requests an uncompressed JSON response with a named publishing client, allows only HTTPS redirects within the same public hostname, and waits 5, 10, 20, 30 and 30 seconds between six attempts (individual network timeouts are additional). Authentication-bearing API requests still reject all redirects. Failures report fixed categories such as `http_403`, `dns_lookup_failed`, `response_is_not_valid_json` or `different_public_data`; response bodies, source contents and token values stay out of logs. These changes address plausible delivery differences and improve diagnosis; the original live failure's specific cause has not yet been established.
- Each upload is an immutable file/content identity. Newer upload timestamps win **per issuing authority + permit number**. Older reports remain historical sources, including when a filename is reused. Omitting a row from a newer file does not retract an old permit automatically. Deliberate retractions need an editor-controlled change and republishing; the upload interface only deletes rejected files that have never been accepted.
- An identical file is not a new permit. Reuploading that exact filename/content identity does not change its already-accepted precedence. A corrected file containing changed values creates a new identity and can update matching permits. Two files uploaded at the same millisecond use the content identity as a deterministic tie breaker; avoid simultaneous conflicting corrections.
- Each successful check saves only the validated public JSON and `last_checked_at` to `public/permits.json` in GitHub. This is a public data history and a meaningful repository update, including days without new permits. GitHub can disable public-repository schedules after 60 days with no repository activity; inspect failed runs and stale check times. Keep normal GitHub failure notifications enabled.
- The job retains accepted source originals and purges rejected originals explicitly marked for removal. Free storage and the 16 MiB private/public snapshot application limit are finite. If a limit is reached, stop and reassess capacity rather than upgrading a plan under this $0 requirement.

## If a report is rejected

The upload page shows a short review message. Download the original privately and check the format. Parser 1.3.0 supports the verified modern 20-column M5 New Permit Report and older 21-column M5 Permit Report layouts. Both require detail and summary reconciliation. Older reports without department/status leave those fields empty and disclose their absence. Recognizable email/telephone tokens are removed from owner, contractor, company and description fields; unexpected contact material in other fields still stops publication. Source originals remain private.

Marriage-license rows in a recognized report are excluded after source totals reconcile. Image-only marriage lists are unsupported and remain Needs review until removed. Signed-in staff can click Delete beside a Needs review file and confirm to erase its private original and remove it from future imports. Published files are protected. A minimal removal marker prevents stale listings or an identical filename/content reupload from ingesting the deleted file. Install the matching Worker and publisher updates, delete between workflow runs, wait about a minute for KV propagation, then start a fresh Run workflow on main. This does not retract previously published permits.


## Verification

`python -m unittest discover -s tests -p test_publisher.py -v` runs synthetic import/transport tests without accounts or credentials. The original-report integration test runs only when the two private originals exist in a sibling `source` folder; those files are not distributed. In this authoring environment, the publisher tests passed with the originals: 41 records and the county totals reconciled. Separate DOM-model reader checks cover setup status, daily freshness, search, filters and export. Native browser rendering, real API tokens, Wrangler deployment, scheduled execution and BLOX embedding still need the owner-led live setup above.

## Primary documentation

- GitHub free public-repository standard runners: https://docs.github.com/en/billing/concepts/product-billing/github-actions
- Schedule time zones: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule
- Scheduling limits and 60-day inactivity: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
- Pages direct upload: https://developers.cloudflare.com/pages/get-started/direct-upload/
- Pages CLI: https://developers.cloudflare.com/workers/wrangler/commands/pages/
- KV free quotas: https://developers.cloudflare.com/kv/platform/pricing/
- KV REST values: https://developers.cloudflare.com/api/resources/kv/subresources/namespaces/subresources/values/methods/update/
- API tokens: https://developers.cloudflare.com/fundamentals/api/get-started/create-token/

Provider documentation checked Nov. 9-10, 2026. No real account credentials appear in this package.
