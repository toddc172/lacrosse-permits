import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cloudflare_client import ServiceError
from upload_contract import upload_metadata,status_record
from fixtures import API,source

class Tests(unittest.TestCase):
    def setUp(self):
        self.api=API();self.ident=self.api.add('2026-09',source())
        self.item=self.api.uploads[0]
    def test_upload_identity_is_validated_and_metadata_is_copied(self):
        meta=upload_metadata(self.item)
        self.assertEqual(meta,self.item['metadata'])
        meta['name']='changed.csv'
        self.assertNotEqual(meta,self.item['metadata'])
        broken=copy.deepcopy(self.item);broken['metadata']['sha256']='0'*64
        with self.assertRaises(ServiceError):upload_metadata(broken)
    def test_unexpected_fields_and_invalid_sizes_are_rejected(self):
        for field,value in [('extra','private'),('bytes',True),('bytes',0),('bytes',8*1024*1024+1)]:
            broken=copy.deepcopy(self.item);broken['metadata'][field]=value
            with self.subTest(field=field,value=value):
                with self.assertRaises(ServiceError):upload_metadata(broken)
    def test_status_distinguishes_accepted_queued_and_rejected(self):
        result=status_record({'files':{self.ident:{}},'public':True}, {'queued':{},'failed':{}}, '2026-11-22T12:00:00Z',rejected={'failed':'Review required.'})
        self.assertTrue(result['publishing_connected'])
        self.assertEqual(result['files'][self.ident]['state'],'published')
        self.assertEqual(result['files']['queued']['state'],'stored')
        self.assertEqual(result['files']['failed'],{'state':'rejected','message':'Review required.'})

if __name__=='__main__':unittest.main()
