# Repository maintenance requirements

## Permits staff uploader

Preserve the exact visible headline:

`Newsroom building and zoning permit uploads`

Do not restore the retired `Newsroom report uploads` headline. Apply the approved
wording to the source HTML/template and every generated standalone or modular
Worker when changing the permits uploader. Preserve the wording in the browser
title when that title repeats the headline. Check the final deployment artifact
before providing it to the user.

The maintained permits page is `cloudflare/permit-upload-page.html`. Rebuild its
constant with `python cloudflare/build_permit_upload_page.py`. Before distributing
a permits Worker update, run `python cloudflare/build_permit_upload_page.py
--check-worker PATH_TO_CANDIDATE_WORKER` against the complete replacement file.
The Nov. 24 attachment contained only PAGE_HTML, not the request handler. Do not
deploy the generated constant as a complete Worker or invent missing backend
code. Obtain the current full Worker before replacing its backend.

This rule concerns the permits staff uploader only. Keep the property transfers
uploader and public database headings specific to their respective products.
See MAINTENANCE.md for the maintenance record.
