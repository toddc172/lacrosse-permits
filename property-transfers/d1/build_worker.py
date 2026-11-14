"""Regenerate the Cloudflare dashboard's single-file Worker after reader edits."""
import json
from pathlib import Path
root=Path(__file__).resolve().parent
html=(root/'preview/index.html').read_text()
(root/'reader.mjs').write_text('export const READER_HTML = '+json.dumps(html)+';\n')
worker=(root/'search-worker.mjs').read_text().replace("import {READER_HTML} from './reader.mjs';",'const READER_HTML = '+json.dumps(html)+';').replace('export async function buildQuery','async function buildQuery').replace('export function pageResult','function pageResult')
(root/'dashboard-worker.js').write_text(worker)
