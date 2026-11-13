import copy
import csv
import gzip
import hashlib
import io
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import transfer_data as data
from publish_transfers import publish_daily, initial_dataset, write_site, ServiceError
from publish_transfers import read_state, store_cache, load_cache, json_bytes, STATE_KEY
from publish_transfers import public_assets


def source(month='2026-09', price='$100,000.00', document='123', extra=None):
    row = dict.fromkeys(data.HEADERS, '')
    row.update({'County':'La Crosse','Document Number':document,'Recorded Date':month[5:]+'-02-'+month[:4],
        'Conveyance Date':month[5:]+'-01-'+month[:4], 'Municipality':'La Crosse, City of',
        'Parcel Number':'\t001-00001-000','Physical Address':'123 MAIN ST','Conveyance Type':'Sale',
        'Sale Price':price,'Estimated Value':'$0.00','Grantor Name':'SOURCE NAME','Grantee Name':'OTHER NAME'})
    if extra: row.update(extra)
    f=io.StringIO(newline='');writer=csv.DictWriter(f,fieldnames=data.HEADERS);writer.writeheader();writer.writerow(row)
    return gzip.compress(f.getvalue().encode(),mtime=0)


class API:
    def __init__(self): self.values={};self.uploads=[]
    def get_json(self,k): return copy.deepcopy(self.values.get(k))
    def get_value(self,k,**kw): return self.values[k]
    def put_json(self,k,v): self.values[k]=copy.deepcopy(v)
    def delete_value(self,k): self.values.pop(k,None);self.uploads=[u for u in self.uploads if u['name']!=k]
    def list_uploads(self): return copy.deepcopy(self.uploads)
    def add(self,month,raw):
        name='RETRHistoricalReport'+month.replace('-','')+'.csv';digest=hashlib.sha256(raw).hexdigest()
        ident=hashlib.sha256((name+'\n'+digest).encode()).hexdigest()
        meta={'schema_version':1,'id':ident,'name':name,'bytes':len(raw),'sha256':digest,'uploaded_at':'2026-11-01T00:00:00Z','media_type':'text/csv'}
        self.values['uploads/'+ident]=raw;self.uploads.append({'name':'uploads/'+ident,'metadata':meta});return ident


class Pages:
    def __init__(self,fail=False): self.fail=fail;self.calls=0
    def publish(self,folder,dataset):
        self.calls+=1
        if self.fail:raise ServiceError('test_deployment_failed')
        assert (Path(folder)/'transfers.json').exists()
        return 'https://example.pages.dev'


class Tests(unittest.TestCase):
    html=Path(__file__).resolve().parents[1]/'web/index.html'
    def run_publish(self,api,pages=None):
        return publish_daily(api,pages or Pages(),self.html,now='2026-11-12T22:00:00Z')
    def test_parse_price_zero_and_leading_zero(self):
        r=data.parse_report(source(price='$0.00'),'RETRHistoricalReport202609.csv')['records'][0]
        self.assertEqual(r['sale_price'],'0.00');self.assertEqual(r['parcel_number'],'001-00001-000')
        self.assertIn('reported_sale_zero_price',r['review_flags'])
    def test_private_fields_excluded(self):
        r=data.parse_report(source(extra={'Grantor Address':'PRIVATE MAIL','Preparer Name':'PRIVATE NAME','Has Financing?':'Yes'}),'RETRHistoricalReport202609.csv')['records'][0]
        self.assertNotIn('PRIVATE',str(r));self.assertEqual(set(r),data.FIELDS)
    def test_wrong_month_and_headers_rejected(self):
        with self.assertRaises(ValueError):data.parse_report(source(),'RETRHistoricalReport202608.csv')
        with self.assertRaises(ValueError):data.parse_report(gzip.compress(b'wrong\n'),'RETRHistoricalReport202609.csv')
    def test_publish_repeat_is_idempotent(self):
        api=API();ident=api.add('2026-09',source());first=self.run_publish(api)
        second=self.run_publish(api)
        self.assertEqual(second['public']['record_count'],1);self.assertFalse(second['changed'])
        self.assertEqual(api.values['control:status']['files'][ident]['state'],'published')
    def test_second_month_preserves_history(self):
        api=API();api.add('2026-09',source());self.run_publish(api)
        api.add('2026-08',source('2026-08',document='122'));r=self.run_publish(api)
        self.assertEqual(r['public']['record_count'],2)
    def test_changed_month_held_and_removable(self):
        api=API();api.add('2026-09',source());self.run_publish(api);prior=copy.deepcopy(api.values['state:current'])
        ident=api.add('2026-09',source(price='$200,000.00'))
        with self.assertRaises(ServiceError):self.run_publish(api)
        self.assertEqual(prior,api.values['state:current']);self.assertEqual(api.values['control:status']['files'][ident]['state'],'rejected')
        api.values['removed/'+ident]={'schema_version':1,'id':ident};self.assertEqual(self.run_publish(api)['public']['record_count'],1)
    def test_invalid_file_holds_batch(self):
        api=API();api.add('2026-09',source());self.run_publish(api);prior=copy.deepcopy(api.values['state:current'])
        ident=api.add('2026-08',gzip.compress(b'unknown\n'))
        with self.assertRaises(ServiceError):self.run_publish(api)
        self.assertEqual(prior,api.values['state:current']);self.assertEqual(api.values['control:status']['files'][ident]['state'],'rejected')
    def test_failed_deploy_never_acknowledged(self):
        api=API();ident=api.add('2026-09',source())
        with self.assertRaises(ServiceError):self.run_publish(api,Pages(True))
        self.assertNotIn('state:current',api.values);self.assertEqual(api.values['control:status']['files'][ident]['state'],'stored')
    def test_cross_month_document_conflict_held(self):
        api=API();api.add('2026-09',source());self.run_publish(api);api.add('2026-08',source('2026-08'))
        with self.assertRaises(ServiceError):self.run_publish(api)
    def test_starter_is_empty_and_allowlisted(self):
        with tempfile.TemporaryDirectory() as folder:
            write_site(folder,initial_dataset(),self.html)
            self.assertEqual({p.name for p in Path(folder).iterdir()},{'index.html','_headers','transfers.json'})
    def test_legacy_state_migrates_without_upload_or_record_loss(self):
        api=API();ident=api.add('2026-09',source())
        self.run_publish(api)
        legacy=read_state(api)
        api.values[STATE_KEY]=copy.deepcopy(legacy)
        first=self.run_publish(api)
        self.assertEqual(api.values[STATE_KEY]['schema_version'],2)
        self.assertEqual(read_state(api)['files'],legacy['files'])
        self.assertEqual(first['public']['records'],legacy['public']['records'])
        self.assertFalse(first['changed'])
        self.assertEqual(api.values['control:status']['files'][ident]['state'],'published')
    def test_state_larger_than_single_value_publishes_and_reloads(self):
        api=API();api.add('2026-09',source())
        def bulk(raw,name):
            parsed=data.parse_report(raw,name)
            base=parsed['records'][0];parsed['records']=[]
            for n in range(35):
                row=copy.deepcopy(base);row['document_number']=str(10000+n);row['id']='la_crosse:'+row['document_number']
                parsed['records'].append(row)
            parsed['controls']['selected_rows']=len(parsed['records'])
            return parsed
        with patch('publish_transfers.MAX_VALUE',64*1024),patch('publish_transfers.CACHE_CHUNK_BYTES',256):
            r=publish_daily(api,Pages(),self.html,now='2026-11-12T22:00:00Z',parser=bulk)
            restored=read_state(api)
            self.assertGreater(len(json_bytes(restored)),64*1024)
            self.assertLess(len(json_bytes(r['public'])),64*1024)
            self.assertEqual(len(restored['public']['records']),35)
            self.assertEqual(api.values[STATE_KEY]['schema_version'],2)
            self.assertTrue(all(len(json_bytes(value))<64*1024 for key,value in api.values.items() if not key.startswith('uploads/')))
            self.assertFalse(publish_daily(api,Pages(),self.html,now='2026-11-12T22:00:00Z',parser=bulk)['changed'])
    def test_missing_cache_chunk_fails_closed(self):
        api=API();api.add('2026-09',source());self.run_publish(api)
        prior=copy.deepcopy(api.values[STATE_KEY]);key=prior['public']['chunks'][0]
        chunk=api.values.pop(key)
        pages=Pages()
        with self.assertRaises(ServiceError):self.run_publish(api,pages)
        self.assertEqual(pages.calls,0);self.assertEqual(api.values[STATE_KEY],prior)
        api.values[key]=chunk
        self.assertEqual(read_state(api)['public']['record_count'],1)
    def test_corrupt_cache_chunk_rejected(self):
        api=API();descriptor=store_cache(api,{'sample':'original'})
        api.values[descriptor['chunks'][0]]['data']='AAAA'
        with self.assertRaises(ServiceError):load_cache(api,descriptor)
    def test_prepared_chunks_do_not_accept_failed_deployment(self):
        api=API();api.add('2026-09',source());self.run_publish(api)
        previous=copy.deepcopy(api.values[STATE_KEY])
        new_id=api.add('2026-08',source('2026-08',document='122'))
        with self.assertRaises(ServiceError):self.run_publish(api,Pages(True))
        self.assertEqual(api.values[STATE_KEY],previous)
        self.assertEqual(read_state(api)['public']['record_count'],1)
        self.assertEqual(api.values['control:status']['files'][new_id]['state'],'stored')
    def test_capacity_error_updates_stale_status(self):
        api=API();api.add('2026-09',source());self.run_publish(api)
        previous=copy.deepcopy(api.values[STATE_KEY]);new_id=api.add('2026-08',source('2026-08',document='122'))
        with patch('publish_transfers.prepare_state',side_effect=ServiceError('private_cache_item_capacity')):
            with self.assertRaises(ServiceError):
                self.run_publish(api)
        self.assertEqual(api.values[STATE_KEY],previous)
        self.assertIn('storage preparation',api.values['control:status']['files'][new_id]['message'])
    def test_public_chunks_reconcile_and_exclude_private_fields(self):
        api=API();api.add('2026-09',source());api.add('2026-08',source('2026-08',document='122'))
        result=self.run_publish(api)['public']
        with patch('publish_transfers.PUBLIC_CHUNK_BYTES',1600):
            index,assets=public_assets(result)
        self.assertEqual(index['schema_version'],2);self.assertNotIn('records',index)
        rows=[]
        import json
        for f in index['data_files']:
            raw=assets[f['file']]
            self.assertEqual(len(raw),f['bytes']);self.assertLessEqual(len(raw),1600)
            self.assertEqual(hashlib.sha256(raw).hexdigest(),f['sha256'])
            part=json.loads(raw);self.assertEqual(len(part['records']),f['record_count'])
            rows.extend(part['records'])
        self.assertEqual(rows,result['records'])
        self.assertTrue(all(set(r)==data.FIELDS for r in rows))
    def test_previous_month_chunks_are_reused_when_month_added(self):
        api=API();api.add('2026-09',source());first=self.run_publish(api)['public']
        _,old_assets=public_assets(first)
        api.add('2026-08',source('2026-08',document='122'));second=self.run_publish(api)['public']
        _,new_assets=public_assets(second)
        for name,raw in old_assets.items():self.assertEqual(new_assets[name],raw)
    def test_record_larger_than_chunk_is_rejected(self):
        api=API();api.add('2026-09',source());result=self.run_publish(api)['public']
        with patch('publish_transfers.PUBLIC_CHUNK_BYTES',20):
            with self.assertRaises(ServiceError):public_assets(result)
    def test_daily_check_reuses_cache_and_record_assets(self):
        api=API();api.add('2026-09',source());first=self.run_publish(api)['public']
        keys={k for k in api.values if k.startswith('state:chunk:')}
        _,old_assets=public_assets(first)
        second=publish_daily(api,Pages(),self.html,now='2026-11-13T22:00:00Z')['public']
        self.assertEqual(keys,{k for k in api.values if k.startswith('state:chunk:')})
        self.assertEqual(public_assets(second)[1],old_assets)
        self.assertEqual(read_state(api)['public']['last_checked_at'],'2026-11-13T22:00:00Z')
    def test_public_reader_chunk_loading_and_failures(self):
        subprocess.run(['node',str(Path(__file__).with_name('test_public_reader.mjs'))],check=True,capture_output=True,text=True)


if __name__=='__main__':unittest.main()
