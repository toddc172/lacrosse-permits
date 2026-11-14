"""Incremental KV -> D1 importer. Default is a pilot of already-published files.
No production Pages deployment and no original deletion occurs in this tool.
"""
import argparse
import datetime as dt
import decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.request import Request, build_opener
from urllib.error import HTTPError, URLError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cloudflare_client import Cloudflare, NoRedirect, ServiceError, json_bytes
from publish_transfers import read_state, upload_metadata, utc, MAX_SOURCE, status_record
from transfer_data import parse_report, validate_unique

class BudgetPause(Exception): pass

class D1:
    def __init__(self,account,database,token,usage=None):
        if not re.fullmatch(r'[a-fA-F0-9]{32}',account or '') or not re.fullmatch(r'[a-fA-F0-9-]{36}',database or '') or not token or any(c.isspace() for c in token):
            raise ServiceError('invalid_d1_configuration')
        self.url=f'https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{database}/query'
        self.token=token;self.opener=build_opener(NoRedirect());self.usage=usage or {'rows_read':0,'rows_written':0}
    def run(self,statements):
        writing=any(not s['sql'].lstrip().upper().startswith('SELECT') for s in statements)
        if self.usage['rows_read']>=1000000 or (writing and self.usage['rows_written']>=35000): raise BudgetPause()
        req=Request(self.url,data=json_bytes({'batch':statements}),method='POST',headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json','User-Agent':'LaCrosseTribune-Transfers-D1/1.0'})
        # No blind retries: an interrupted write may already have committed.
        # Staged inserts and activation are idempotent on the next workflow run.
        try:
            with self.opener.open(req,timeout=45) as response:raw=response.read(8*1024*1024+1)
            if len(raw)>8*1024*1024:raise ServiceError('d1_response_capacity')
            body=json.loads(raw)
            if body.get('success') is not True or not isinstance(body.get('result'),list):raise ServiceError('d1_query_rejected')
            result=body['result']
            if not result or any(r.get('success') is not True for r in result):raise ServiceError('d1_query_rejected')
            for r in result:
                for key in ['rows_read','rows_written']:self.usage[key]+=int(r.get('meta',{}).get(key,0))
            return [r.get('results',[]) for r in result]
        except HTTPError as error:
            status=error.code;error.close();raise ServiceError(f'd1_http_{status}') from None
        except (URLError,TimeoutError,OSError,ValueError):raise ServiceError('d1_transport_or_response_error') from None
    def query(self,sql,params=()):return self.run([{'sql':sql,'params':list(params)}])[0]

def digest(value):return hashlib.sha256(json_bytes(value)).hexdigest()

def record_values(row,batch_id):
    amount=row['sale_price']
    cents=int(decimal.Decimal(amount)*100) if amount else None
    if cents is not None and abs(cents)>9007199254740991:raise ServiceError('price_out_of_range')
    search=' '.join(row[k] for k in ['grantor_name','grantee_name','property_address_reported','parcel_number','municipality','county'])
    return [row['id'],batch_id,row['county'],row['municipality'],row['recorded_date'],row['conveyance_type'],row['property_use'],row['parcel_number'],cents,json_bytes(row).decode().strip(),digest(row),search]

def import_month(db,metadata,records,now):
    validate_unique(records)
    records=sorted(records,key=lambda r:r['id'])
    month=records[0]['source_month'] if records else ''
    if not month or any(r['source_month']!=month or r['recorded_date'][:7]!=month for r in records):raise ServiceError('invalid_batch_period')
    ident=metadata['id'];pairs=[[r['id'],digest(r)] for r in records];fingerprint=digest(pairs)
    existing=db.query('SELECT * FROM batches WHERE id=? OR source_month=?',[ident,month])
    if existing:
        batch=existing[0]
        if batch['id']!=ident or batch['record_hash']!=fingerprint or batch['expected_count']!=len(records):raise ServiceError('d1_month_conflict_review_required')
        if batch['status']=='active':return False
    else:
        db.query('INSERT INTO batches(id,source_month,source_name,source_sha,record_hash,expected_count,metadata_json) VALUES(?,?,?,?,?,?,?)',
                 [ident,month,metadata['name'],metadata['sha256'],fingerprint,len(records),json_bytes(metadata).decode().strip()])
    saved=db.query('SELECT transfer_id,record_hash FROM transfers WHERE batch_id=? ORDER BY transfer_id',[ident])
    known={r['transfer_id']:r['record_hash'] for r in saved};expected=dict(pairs)
    if any(k not in expected or expected[k]!=v for k,v in known.items()):raise ServiceError('d1_staged_content_conflict')
    pending=[row for row in records if row['id'] not in known]
    for start in range(0,len(pending),90):
        ids=[row['id'] for row in pending[start:start+90]]
        conflict=db.query('SELECT transfer_id FROM transfers WHERE batch_id<>? AND transfer_id IN ('+','.join('?' for _ in ids)+')',[ident]+ids)
        if conflict:raise ServiceError('d1_document_conflict_review_required')
    for start in range(0,len(pending),20):
        # Each statement has 12 parameters; batching cuts request overhead.
        # Failed/paused batches resume using the saved record hashes.
        db.run([{'sql':'INSERT INTO transfers(transfer_id,batch_id,county,municipality,recorded_date,conveyance_type,property_use,parcel_number,sale_price_cents,record_json,record_hash,search_text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                 'params':record_values(row,ident)} for row in pending[start:start+20]])
    actual=db.query('SELECT transfer_id,record_hash FROM transfers WHERE batch_id=? ORDER BY transfer_id',[ident])
    if digest([[r['transfer_id'],r['record_hash']] for r in actual])!=fingerprint:raise ServiceError('d1_batch_reconciliation_failed')
    current=json.loads(db.query("SELECT value FROM settings WHERE name='public'")[0]['value']);old_revision=current['revision']
    summary=dict(current)
    summary.update(revision=digest([old_revision,ident,fingerprint]),record_count=current['record_count']+len(records),source_count=current['source_count']+1,generated_at=now)
    dates=[r['recorded_date'] for r in records]
    summary['oldest_recorded_date']=min([min(dates)]+([current['oldest_recorded_date']] if current['oldest_recorded_date'] else []))
    summary['latest_recorded_date']=max([max(dates)]+([current['latest_recorded_date']] if current['latest_recorded_date'] else []))
    summary['options']={field:sorted(set(current['options'][field])|{r[field] for r in records if r[field]}) for field in current['options']}
    summary['included_months']=sorted(set(current.get('included_months',[]))|{month})
    summary['methodology']='Wisconsin DOR real estate transfer returns for six counties. All transfer types are retained. Values, names and locations are as reported.'
    # A competing writer cannot activate against an obsolete summary revision.
    db.query("UPDATE batches SET public_metadata=?,activated_at=?,status='active' WHERE id=? AND status='staging' AND json_extract((SELECT value FROM settings WHERE name='public'),'$.revision')=?",
             [json_bytes(summary).decode().strip(),now,ident,old_revision])
    check=db.query('SELECT status FROM batches WHERE id=?',[ident])
    if check[0]['status']!='active':raise ServiceError('d1_activation_revision_conflict')
    return True

def clean_removed_staging(api,db):
    # Honor the existing staff deletion marker only for never-active batches.
    # This releases a rejected month's reservation for a corrected source.
    for batch in db.query("SELECT id FROM batches WHERE status='staging'"):
        ident=batch['id'];marker=api.get_json('removed/'+ident)
        if marker is None:continue
        if not isinstance(marker,dict) or marker.get('schema_version')!=1 or marker.get('id')!=ident:raise ServiceError('invalid_removal_marker')
        while db.query('SELECT rowid FROM transfers WHERE batch_id=? LIMIT 1',[ident]):
            db.query("DELETE FROM transfers WHERE rowid IN (SELECT t.rowid FROM transfers t JOIN batches b ON b.id=t.batch_id WHERE t.batch_id=? AND b.status='staging' LIMIT 50)",[ident])
        db.query("DELETE FROM batches WHERE id=? AND status='staging'",[ident])

def synchronize(api,db,mode='pilot',max_months=3,now=None):
    now=now or dt.datetime.now(dt.timezone.utc).isoformat();clock=utc(now)
    clean_removed_staging(api,db)
    accepted={r['id']:r for r in db.query("SELECT id,metadata_json FROM batches WHERE status='active'")}
    if mode=='pilot':
        old=read_state(api)
        sources=[(entry['metadata'],entry['records']) for entry in old['files'].values()]
        print(f'Pilot baseline: {len(sources)} published sources and {sum(len(rows) for _,rows in sources)} records.')
    else:
        sources=[]
        for item in api.list_uploads():
            meta=upload_metadata(item)
            if meta['id'] in accepted:continue
            if api.get_json('removed/'+meta['id']) is not None:continue
            sources.append((meta,None))
    count=0;issues={};uploaded={meta['id']:meta for meta,_ in sources}
    for meta,cached in sorted(sources,key=lambda item:item[0]['name']):
        ident=meta['id']
        if ident in accepted:continue
        if count>=max_months:break
        if (clock-utc(meta['uploaded_at'])).total_seconds()<90:
            issues[ident]='Waiting for a later publishing run after this recent upload.';continue
        try:
            if cached is None:
                raw=api.get_value('uploads/'+ident,limit=MAX_SOURCE)
                if len(raw)!=meta['bytes'] or hashlib.sha256(raw).hexdigest()!=meta['sha256']:raise ServiceError('source_content_hash_mismatch')
                records=parse_report(raw,meta['name'])['records']
            else:records=cached
            if import_month(db,meta,records,now):
                count+=1
                print(f'Activated {meta["name"]}: {len(records)} records.')
            accepted[ident]={'id':ident,'metadata_json':json_bytes(meta).decode()}
        except Exception as error:
            if mode=='production':
                bad=isinstance(error,ValueError) or (isinstance(error,ServiceError) and error.code in {'d1_month_conflict_review_required','d1_document_conflict_review_required','d1_staged_content_conflict','d1_batch_reconciliation_failed'})
                status=status_record({'files':accepted,'public':bool(accepted)},uploaded,now)
                status['files'][ident]={'state':'rejected' if bad else 'stored','message':
                    'This source needs editorial review. Previously accepted records remain available.' if bad else
                    'The D1 import paused or was interrupted. Staged rows remain private; check the workflow before retrying.'}
                api.put_json('control:status',status)
            # Fail closed: never skip a failed file silently and declare success.
            raise
    if mode=='production':
        status=status_record({'files':accepted,'public':bool(accepted)},uploaded,now)
        for ident,message in issues.items():status['files'][ident]={'state':'stored','message':message}
        api.put_json('control:status',status)
    return count

def audit_pilot(api,db):
    old=read_state(api);active={r['id']:r for r in db.query("SELECT id,record_hash,expected_count FROM batches WHERE status='active'")}
    expected=0;missing=0
    for ident,entry in old['files'].items():
        rows=entry['records'];expected+=len(rows)
        if ident not in active:missing+=1;continue
        fingerprint=digest([[r['id'],digest(r)] for r in sorted(rows,key=lambda r:r['id'])])
        if active[ident]['record_hash']!=fingerprint or active[ident]['expected_count']!=len(rows):raise ServiceError('pilot_source_reconciliation_failed')
    summary=json.loads(db.query("SELECT value FROM settings WHERE name='public'")[0]['value'])
    print(f'Pilot audit: expected {expected} static-published records; D1 has {summary["record_count"]}. Missing source months: {missing}.')
    if not missing and (set(active)!=set(old['files']) or summary['record_count']!=expected):raise ServiceError('pilot_total_reconciliation_failed')
    return not missing

def report_coverage(db):
    summary=json.loads(db.query("SELECT value FROM settings WHERE name='public'")[0]['value'])
    latest=summary.get('latest_recorded_date')
    if not latest:return
    included=set(summary.get('included_months',[]));year,month=2021,1;missing=[]
    while f'{year:04d}-{month:02d}'<=latest[:7]:
        period=f'{year:04d}-{month:02d}'
        if period not in included:missing.append(period)
        month+=1
        if month==13:year+=1;month=1
    print(f'D1 totals: {summary["record_count"]} records, {summary["source_count"]} source months. Missing months since 2021-01: {len(missing)}.')
    if missing:print('Missing source months: '+', '.join(missing))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--mode',choices=['pilot','production'],default='pilot');parser.add_argument('--max-months',type=int,default=3);parser.add_argument('--init',action='store_true');parser.add_argument('--audit',action='store_true');args=parser.parse_args()
    if not 1<=args.max_months<=12:parser.error('max-months must be 1 to 12')
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('TRANSFERS_PUBLIC_REPOSITORY')!='true':raise ServiceError('run_only_in_public_repository_workflow')
    api=Cloudflare(os.environ.get('CLOUDFLARE_ACCOUNT_ID',''),os.environ.get('CLOUDFLARE_KV_NAMESPACE_ID',''),os.environ.get('CLOUDFLARE_API_TOKEN',''))
    key='d1:usage:'+dt.datetime.now(dt.timezone.utc).date().isoformat();usage=api.get_json(key) or {'rows_read':0,'rows_written':0}
    db=D1(api.account,os.environ.get('TRANSFERS_D1_DATABASE_ID',''),os.environ.get('TRANSFERS_D1_API_TOKEN',''),usage)
    try:
        if args.init:db.query(Path(__file__).with_name('schema.sql').read_text())
        count=synchronize(api,db,args.mode,args.max_months)
        if args.audit:
            if args.mode!='pilot':raise ServiceError('audit_is_for_pilot_only')
            audit_pilot(api,db)
        report_coverage(db)
        print(f'D1 {args.mode}: activated {count} source months. Import usage: {db.usage["rows_read"]} rows read; {db.usage["rows_written"]} rows written today.')
    except BudgetPause:
        print('D1 import paused at its conservative daily budget. Staged rows remain hidden and will resume after the UTC daily reset.')
    except Exception as exc:
        print('D1 import stopped: '+(exc.code if isinstance(exc,ServiceError) else 'editorial_review_required'),file=sys.stderr)
        return 1
    finally:api.put_json(key,db.usage)
    return 0

if __name__=='__main__':
    try:sys.exit(main())
    except ServiceError as error:
        print('D1 setup or transport stopped: '+error.code,file=sys.stderr);sys.exit(1)
