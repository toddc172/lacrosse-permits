import copy
import gzip
import hashlib
import json
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

D1ROOT=Path(__file__).resolve().parents[1];ROOT=D1ROOT.parent
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(D1ROOT))
from sync_d1 import import_month,synchronize,BudgetPause,clean_removed_staging,D1
from cloudflare_client import ServiceError
from transfer_data import parse_report,public_dataset,PARSER_VERSION
from deploy_reader import write_reader,api_origin
helpers=runpy.run_path(str(ROOT/'tests/test_transfers.py'))
source=helpers['source'];API=helpers['API']
NOW='2026-11-13T22:00:00Z'

class SQLiteD1:
    def __init__(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.db.executescript((D1ROOT/'schema.sql').read_text());self.pause=False
    def run(self,statements):
        if self.pause and any(s['sql'].startswith('UPDATE batches SET public_metadata') for s in statements):raise BudgetPause()
        with self.db:
            return [[dict(r) for r in self.db.execute(s['sql'],s.get('params',[])).fetchall()] for s in statements]
    def query(self,sql,params=()):return self.run([{'sql':sql,'params':params}])[0]
    def meta(self):return json.loads(self.query("SELECT value FROM settings WHERE name='public'")[0]['value'])

def entry(month='2026-09',document='123',price='$100,000.00',extra=None):
    api=API();raw=source(month,price,document,extra);ident=api.add(month,raw);meta=api.uploads[0]['metadata'];parsed=parse_report(raw,meta['name'])
    return meta,parsed['records'],api

def plans(queries):
    result=subprocess.run(['node',str(D1ROOT/'tests/query_plan.mjs')],input=json.dumps({'queries':queries}),capture_output=True,text=True,check=True)
    return json.loads(result.stdout)

class Tests(unittest.TestCase):
    def setUp(self):self.db=SQLiteD1()
    def test_activation_counts_and_exact_zero(self):
        meta,records,_=entry(price='$0.00');self.assertTrue(import_month(self.db,meta,records,NOW))
        self.assertEqual(self.db.meta()['record_count'],1);self.assertEqual(self.db.query('SELECT sale_price_cents FROM transfers')[0]['sale_price_cents'],0)
    def test_repeat_skips_all_writes(self):
        meta,records,_=entry();import_month(self.db,meta,records,NOW);changes=self.db.db.total_changes
        self.assertFalse(import_month(self.db,meta,records,NOW));self.assertEqual(self.db.db.total_changes,changes)
    def test_staged_records_hidden_until_reconciliation(self):
        meta,records,_=entry();self.db.pause=True
        with self.assertRaises(BudgetPause):import_month(self.db,meta,records,NOW)
        self.assertEqual(self.db.meta()['record_count'],0)
        self.assertEqual(self.db.query("SELECT count(*) n FROM transfers t JOIN batches b ON b.id=t.batch_id WHERE b.status='active'")[0]['n'],0)
        self.db.pause=False;import_month(self.db,meta,records,NOW)
        self.assertEqual(self.db.query('SELECT count(*) n FROM transfers')[0]['n'],1)
    def test_changed_month_is_held(self):
        meta,records,_=entry();import_month(self.db,meta,records,NOW);m2,r2,_=entry(price='$200,000.00')
        with self.assertRaises(ServiceError):import_month(self.db,m2,r2,NOW)
        self.assertEqual(self.db.meta()['record_count'],1)
    def test_cross_month_document_is_held(self):
        meta,records,_=entry();import_month(self.db,meta,records,NOW);m2,r2,_=entry('2026-08')
        with self.assertRaises(ServiceError):import_month(self.db,m2,r2,NOW)
        self.assertEqual(self.db.meta()['source_count'],1)
    def test_pilot_leaves_upload_status_and_state_untouched(self):
        meta,records,api=entry();parsed=parse_report(api.values['uploads/'+meta['id']],meta['name'])
        old={'schema_version':1,'files':{meta['id']:{'metadata':meta,'modified':meta['uploaded_at'],'parser_version':PARSER_VERSION,'records':records,'controls':parsed['controls']}},'public':None}
        api.values['state:current']=old;before=copy.deepcopy(api.values)
        self.assertEqual(synchronize(api,self.db,now=NOW),1);self.assertEqual(api.values,before)
    def test_production_status_preserves_published_protection(self):
        meta,records,api=entry();synchronize(api,self.db,mode='production',now=NOW)
        self.assertEqual(api.values['control:status']['files'][meta['id']]['state'],'published')
        self.assertEqual(synchronize(api,self.db,mode='production',now=NOW),0)
    def test_query_filters_fts_and_invalid_input(self):
        meta,records,_=entry(extra={'Grantor Name':'O\'BRIEN, RENÉE','Physical Address':'123 MAIN ST'});import_month(self.db,meta,records,NOW)
        rev=self.db.meta()['revision']
        queries=[{'rev':rev},{'rev':rev,'q':'rene main'},{'rev':rev,'q':'" OR 1=1 --'}, {'rev':rev,'county':'Monroe'}, {'rev':rev,'min':'200000'}, {'rev':rev,'limit':'100000'}, {'rev':rev,'sort':'DROP TABLE transfers'}, {'rev':rev,'from':'2026-09-99'}]
        results=plans(queries)
        self.assertEqual(len(self.db.query(results[0]['sql'],results[0]['bind'])),1)
        self.assertEqual(len(self.db.query(results[1]['sql'],results[1]['bind'])),1)
        self.assertEqual(len(self.db.query(results[2]['sql'],results[2]['bind'])),0)
        self.assertEqual(len(self.db.query(results[3]['sql'],results[3]['bind'])),0)
        self.assertEqual(len(self.db.query(results[4]['sql'],results[4]['bind'])),0)
        for r in results[5:]:self.assertIn('error',r)
    def test_preview_cutover_assets_are_allowlisted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            write_reader(temp,'https://example.owner.workers.dev')
            self.assertEqual({p.name for p in Path(temp).iterdir()},{'index.html','_headers','d1-config.json'})
            self.assertIn('https://example.owner.workers.dev',(Path(temp)/'_headers').read_text())
        with self.assertRaises(ServiceError):api_origin('http://not-https.example')
    def test_keyset_pagination_for_every_sort(self):
        meta,records,_=entry();base=records[0];rows=[]
        for i in range(61):
            row=dict(base,id='la_crosse:'+str(1000+i),document_number=str(1000+i),sale_price='' if i%7==0 else str((i%4)*10000)+'.00')
            rows.append(row)
        import_month(self.db,meta,rows,NOW);rev=self.db.meta()['revision']
        for sort in ['new','old','high']:
            cursor=None;ids=[]
            while True:
                query={'rev':rev,'sort':sort}
                if cursor:query['cursor']=cursor
                plan=plans([query])[0];found=self.db.query(plan['sql'],plan['bind'])
                result=subprocess.run(['node',str(D1ROOT/'tests/query_plan.mjs')],input=json.dumps({'kind':'page','plan':plan,'rows':found}),capture_output=True,text=True,check=True)
                page=json.loads(result.stdout);ids.extend(r['id'] for r in page['records']);cursor=page['next_cursor']
                if not cursor:break
            self.assertEqual(len(ids),61);self.assertEqual(len(set(ids)),61)
    def test_staged_fulltext_results_are_not_public(self):
        meta,records,_=entry();self.db.pause=True
        with self.assertRaises(BudgetPause):import_month(self.db,meta,records,NOW)
        plan=plans([{'rev':'empty','q':'source'}])[0]
        self.assertEqual(self.db.query(plan['sql'],plan['bind']),[])
    def test_removed_staging_releases_month_and_fts_rows(self):
        meta,records,api=entry();self.db.pause=True
        with self.assertRaises(BudgetPause):import_month(self.db,meta,records,NOW)
        self.db.pause=False;api.values['removed/'+meta['id']]={'schema_version':1,'id':meta['id']}
        clean_removed_staging(api,self.db)
        self.assertEqual(self.db.query('SELECT * FROM batches'),[])
        self.assertEqual(self.db.query("SELECT rowid FROM transfer_search WHERE transfer_search MATCH 'source'"),[])
        m2,r2,_=entry(price='$200,000.00');self.assertTrue(import_month(self.db,m2,r2,NOW))
    def test_removal_marker_cannot_retract_active_month(self):
        meta,records,api=entry();import_month(self.db,meta,records,NOW)
        api.values['removed/'+meta['id']]={'schema_version':1,'id':meta['id']};clean_removed_staging(api,self.db)
        self.assertEqual(self.db.meta()['record_count'],1);self.assertEqual(self.db.query('SELECT count(*) n FROM transfers')[0]['n'],1)
    def test_test_reader_deployment_and_verification(self):
        import deploy_reader
        import types
        meta,records,_=entry();import_month(self.db,meta,records,NOW)
        origin='https://test.owner.workers.dev';holder={}
        class Adapter:
            account='a'*32;token='fake-test-token'
            def project(self,name):return {'name':'reader','hostname':'reader.pages.dev','branch':'main'}
        def public(url):return self.db.meta() if url.endswith('/metadata') else {'mode':'d1','api_base':origin}
        def deploy(argv,**kwargs):holder['html']=(Path(kwargs['cwd'])/'index.html').read_bytes();return types.SimpleNamespace(returncode=0)
        class Response:
            headers={}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return holder['html']
        opener=types.SimpleNamespace(open=lambda *args,**kwargs:Response())
        with patch.dict('os.environ',{'GITHUB_ACTIONS':'true','TRANSFERS_PUBLIC_REPOSITORY':'true','TRANSFERS_D1_API_URL':origin}),patch.object(deploy_reader,'Cloudflare',lambda *args:Adapter()),patch.object(deploy_reader,'D1',lambda *args:self.db),patch.object(deploy_reader,'read_public',public),patch.object(deploy_reader.subprocess,'run',deploy),patch.object(deploy_reader,'build_opener',lambda *args:opener):
            deploy_reader.main()
        self.assertIn(origin.encode(),holder['html']);self.assertNotIn(b'fake-test-token',holder['html'])
    def test_rest_batch_contract_and_usage_pause(self):
        client=D1('a'*32,'11111111-1111-1111-1111-111111111111','fake-token')
        captured=[]
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return json.dumps({'success':True,'result':[{'success':True,'results':[{'n':1}],'meta':{'rows_read':2,'rows_written':3}}]}).encode()
        class Opener:
            def open(self,request,**kwargs):captured.append(request);return Response()
        client.opener=Opener()
        self.assertEqual(client.query('SELECT ? AS n',[1]),[{'n':1}])
        self.assertEqual(json.loads(captured[0].data),{'batch':[{'sql':'SELECT ? AS n','params':[1]}]})
        self.assertEqual(captured[0].get_header('Content-type'),'application/json')
        self.assertEqual(client.usage,{'rows_read':2,'rows_written':3})
        client.usage['rows_written']=35000
        with self.assertRaises(BudgetPause):client.query('INSERT INTO t VALUES(?)',[1])
        self.assertEqual(len(captured),1)

if __name__=='__main__':unittest.main()
