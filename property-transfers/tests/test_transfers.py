import sys
import gzip
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import transfer_data as data
from fixtures import source

class Tests(unittest.TestCase):
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

if __name__=="__main__":unittest.main()
