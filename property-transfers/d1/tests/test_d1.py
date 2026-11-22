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
from sync_d1 import import_month,synchronize,BudgetPause,clean_removed_staging,D1,run_batches
from cloudflare_client import ServiceError
from transfer_data import parse_report
helpers=runpy.run_path(str(ROOT/'tests/fixtures.py'))
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
    def test_until_budget_runs_successive_batches(self):
        with patch('sync_d1.synchronize',side_effect=[3,3,2]) as sync:
            self.assertEqual(run_batches(None,None,'production',3,True),8)
            self.assertEqual(sync.call_count,3)
    def test_until_budget_stops_immediately_at_safeguard(self):
        with patch('sync_d1.synchronize',side_effect=[3,BudgetPause()]) as sync:
            with self.assertRaises(BudgetPause):run_batches(None,None,'production',3,True)
            self.assertEqual(sync.call_count,2)
    def test_manual_single_batch_remains_supported(self):
        with patch('sync_d1.synchronize',return_value=3) as sync:
            self.assertEqual(run_batches(None,None,'production',3,False),3)
            self.assertEqual(sync.call_count,1)


    def test_production_has_no_legacy_state_dependency(self):
        import sync_d1
        meta,records,api=entry()
        original=api.values['uploads/'+meta['id']]
        api.account='a'*32
        api.values['state:current']={'retired':'must remain untouched'}
        api.values['d1:migration:complete']={'retired':'must remain untouched'}
        read=api.get_json
        def guarded(key):
            if key in {'state:current','d1:migration:complete'}:raise AssertionError('Retired state must not be read')
            return read(key)
        api.get_json=guarded
        self.db.usage={'rows_read':0,'rows_written':0}
        with patch.dict('os.environ',{'GITHUB_ACTIONS':'true','TRANSFERS_PUBLIC_REPOSITORY':'true','TRANSFERS_D1_DATABASE_ID':'11111111-1111-1111-1111-111111111111'}),patch.object(sync_d1,'Cloudflare',lambda *args:api),patch.object(sync_d1,'D1',lambda *args:self.db),patch.object(sys,'argv',['sync_d1.py']):
            self.assertEqual(sync_d1.main(),0)
            self.assertEqual(sync_d1.main(),0)
        self.assertEqual(self.db.meta()['record_count'],1)
        self.assertEqual(api.values['uploads/'+meta['id']],original)
        self.assertEqual(api.values['state:current'],{'retired':'must remain untouched'})
        self.assertEqual(api.values['d1:migration:complete'],{'retired':'must remain untouched'})
        self.assertEqual(api.values['control:status']['files'][meta['id']]['state'],'published')
        self.assertEqual(len([k for k in api.values if k.startswith('d1:usage:')]),1)

    def test_failed_source_is_reported_and_later_file_is_not_imported(self):
        api=API()
        broken=api.add('2026-08',gzip.compress(b'wrong headers\n'))
        later=api.add('2026-09',source())
        originals=copy.deepcopy(api.values)
        with self.assertRaises(ValueError):synchronize(api,self.db,now=NOW)
        self.assertEqual(self.db.meta()['record_count'],0)
        status=api.values['control:status']['files']
        self.assertEqual(status[broken]['state'],'rejected')
        self.assertEqual(status[later]['state'],'stored')
        for key,value in originals.items():self.assertEqual(api.values[key],value)

    def test_budget_pause_keeps_source_and_reports_stored(self):
        meta,records,api=entry();self.db.pause=True
        original=api.values['uploads/'+meta['id']]
        with self.assertRaises(BudgetPause):synchronize(api,self.db,now=NOW)
        self.assertEqual(api.values['control:status']['files'][meta['id']]['state'],'stored')
        self.assertEqual(api.values['uploads/'+meta['id']],original)
        self.assertEqual(self.db.meta()['record_count'],0)
        self.db.pause=False
        self.assertEqual(synchronize(api,self.db,now=NOW),1)
        self.assertEqual(self.db.meta()['record_count'],1)

    def test_retired_pilot_command_is_rejected_before_any_network_access(self):
        import sync_d1
        import contextlib
        import io
        with patch.object(sys,'argv',['sync_d1.py','--mode','pilot']),patch.object(sync_d1,'Cloudflare',side_effect=AssertionError('No client expected')),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:sync_d1.main()
        self.assertEqual(error.exception.code,2)

if __name__=='__main__':unittest.main()
