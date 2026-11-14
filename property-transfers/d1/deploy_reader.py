"""Deploy the D1 reader to the existing TEST Pages project."""
import hashlib
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request,build_opener

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from cloudflare_client import Cloudflare,ServiceError,NoRedirect,json_bytes
from publish_transfers import public_body,WRANGLER
from sync_d1 import D1

def api_origin(value):
    if not re.fullmatch(r'https://[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev/?',value or ''):raise ServiceError('invalid_d1_api_url')
    return value.rstrip('/')

def read_public(url):
    # Public, unauthenticated verification. Credentials never leave API hosts.
    try:
        with build_opener(NoRedirect()).open(Request(url,headers={'Accept-Encoding':'identity','Cache-Control':'no-cache'}),timeout=25) as response:return json.loads(public_body(response))
    except Exception:raise ServiceError('public_verification_unavailable') from None

def write_reader(folder,origin):
    folder=Path(folder);html=Path(__file__).with_name('preview').joinpath('index.html').read_text()
    marker='const API_BASE = ""; // SET_API_BASE'
    if html.count(marker)!=1:raise ServiceError('invalid_reader_template')
    html=html.replace(marker,'const API_BASE = '+json.dumps(origin)+'; // SET_API_BASE')
    script=re.search(r'<script>([\s\S]*?)</script>',html)[1]
    digest=base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    headers="/*\n  Content-Security-Policy: default-src 'none'; script-src 'sha256-"+digest+"'; style-src 'unsafe-inline'; connect-src 'self' "+origin+"; base-uri 'none'; object-src 'none'; frame-ancestors 'self' https://lacrossetribune.com https://*.lacrossetribune.com\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: no-referrer\n  Cache-Control: no-cache\n"
    (folder/'index.html').write_text(html);(folder/'_headers').write_text(headers)
    (folder/'d1-config.json').write_bytes(json_bytes({'mode':'d1','api_base':origin}))

def main():
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('TRANSFERS_PUBLIC_REPOSITORY')!='true':raise ServiceError('run_only_in_public_repository_workflow')
    origin=api_origin(os.environ.get('TRANSFERS_D1_API_URL',''))
    api=Cloudflare(os.environ.get('CLOUDFLARE_ACCOUNT_ID',''),os.environ.get('CLOUDFLARE_KV_NAMESPACE_ID',''),os.environ.get('CLOUDFLARE_API_TOKEN',''))
    db=D1(api.account,os.environ.get('TRANSFERS_D1_DATABASE_ID',''),os.environ.get('TRANSFERS_D1_API_TOKEN',''))
    project=api.project(os.environ.get('CLOUDFLARE_PAGES_PROJECT',''))
    expected=json.loads(db.query("SELECT value FROM settings WHERE name='public'")[0]['value'])
    actual=read_public(origin+'/metadata')
    if actual!=expected or not actual.get('record_count'):raise ServiceError('d1_preview_counts_or_revision_mismatch')
    with tempfile.TemporaryDirectory(prefix='d1-reader-') as temp:
        folder=Path(temp);write_reader(folder,origin)
        env=dict(os.environ,CLOUDFLARE_ACCOUNT_ID=api.account,CLOUDFLARE_API_TOKEN=api.token,WRANGLER_SEND_METRICS='false',CI='true')
        result=subprocess.run(['npx','--yes','wrangler@'+WRANGLER,'pages','deploy',str(folder),'--project-name',project['name'],'--branch',project['branch'],'--commit-dirty=true'],cwd=folder,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=360)
        if result.returncode:raise ServiceError('d1_reader_deployment_failed')
        wanted=(folder/'index.html').read_bytes()
        for attempt in range(6):
            try:
                config=read_public('https://'+project['hostname']+'/d1-config.json')
                if config!={'mode':'d1','api_base':origin}:raise ServiceError('reader_config_not_verified')
                with build_opener(NoRedirect()).open(Request('https://'+project['hostname']+'/?d1='+hashlib.sha256(wanted).hexdigest(),headers={'Accept-Encoding':'identity','Cache-Control':'no-cache'}),timeout=25) as response:body=public_body(response)
                if body==wanted:break
            except ServiceError:pass
            if attempt==5:raise ServiceError('d1_reader_verification_failed')
            time.sleep(min(30,5*2**attempt))
    print('D1 reader deployed and verified at the existing Pages address. BLOX embed is unchanged.')

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('D1 test-reader deployment stopped: '+(exc.code if isinstance(exc,ServiceError) else 'review_required'),file=sys.stderr);sys.exit(1)
